"""Generation. OCI Generative AI is the only model service.

Every path is grounded: requirement drafting and questionnaire fill look at the
approved answer library first and the retrieval index second, and each result
carries the provenance of what it drew from.
"""
from __future__ import annotations

import asyncio
import logging

from . import answers, classifier, llm, prompts, retrieval
from .config import cfg

log = logging.getLogger("harald.generation")

MODULE_TITLES = {
    "FIN": "Financial Management",
    "HCM": "Human Resources",
    "PAYROLL": "Payroll",
    "PROC": "Procurement",
    "BUDGET": "Budget",
    "INV": "Inventory and Asset Management",
    "TECH": "Technical Approach",
    "GENERAL": "General",
}

DEFAULT_CODES = ["Standard", "Configuration", "Modification", "Third Party",
                 "Not Available", "Future Release"]

# Codes that mean "we can't / won't" — never pick these just because the library missed.
_NEGATIVE_CODE_HINTS = (
    "not available", "unavailable", "n/a", "na", "none", "no",
    "does not support", "cannot", "can't", "unable", "out of scope",
)

_CANNOT_DO_HINTS = (
    "cannot complete", "can't complete", "cannot meet", "can't meet",
    "cannot support", "can't support", "unable to", "not able to",
    "do not have the ability", "don't have the ability",
    "we cannot", "we can't", "iteria cannot", "iteria can't",
    "no capability", "not supported by iteria",
)

HUMAN_FLAG = "[NEEDS HUMAN"


def _exemplars(chunks: list[dict]) -> str:
    if not chunks:
        return (
            "[no close match in iteria's approved library. "
            "This is NOT a reason to decline the requirement. "
            "Draft a constructive answer from iteria's standard public-sector "
            "Oracle Cloud Fusion practice and documented Oracle Fusion capabilities. "
            "Mark only client-specific unknowns as [NEEDS HUMAN: ...]. "
            "Do not choose Not Available / cannot-complete language.]"
        )
    return "\n\n---\n\n".join(
        f"[iteria past response {i + 1}: {c['client']} ({c['outcome']}), "
        f"{MODULE_TITLES.get(c['module'], c['module'])} / {c['section']}]\n{c['text']}"
        for i, c in enumerate(chunks)
    )


def _grounding(question: str, module: str | None) -> tuple[str, list[dict], dict | None]:
    """Approved answer library first, retrieval second. Returns the context block,
    the provenance list, and the library match when one is strong."""
    match = answers.best_match(question, module)
    chunks = retrieval.retrieve(question, module, k=5)

    parts: list[str] = []
    sources: list[dict] = []
    if match:
        label = "strong match" if match["strong"] else "related"
        parts.append(f"[iteria approved answer ({label})]\n{match['answer_text']}")
        sources.append({"kind": "answer", "ans_id": match["ans_id"],
                        "question": match["question_canonical"], "score": match["score"]})
    if chunks:
        parts.append(_exemplars(chunks))
        sources.extend(
            {"kind": "chunk", "chunk_id": c["chunk_id"], "client": c["client"],
             "outcome": c["outcome"], "module": c["module"], "section": c["section"],
             "score": c["score"]}
            for c in chunks
        )
    context = "\n\n---\n\n".join(parts) if parts else _exemplars([])
    return context, sources, (match if match and match["strong"] else None)


async def draft_requirement(req_text: str, module: str, client: str,
                            state: str | None = None) -> dict:
    context, sources, strong = _grounding(req_text, module)
    user = (
        f"CLIENT: {client}{', ' + state if state else ''}\n"
        f"MODULE: {MODULE_TITLES.get(module, module)}\n"
        f"REQUIREMENT (the agency's exact wording):\n{req_text}\n\n"
        f"ITERIA'S OWN MATERIAL (match this voice, reuse this substance, never copy it verbatim):\n"
        f"{context}\n\n"
        "Write iteria's response to this requirement now. One to three tight paragraphs, "
        "specific to this client."
    )
    text = await llm.complete(prompts.DRAFT_SYSTEM, user, cfg.draft_model, max_tokens=1200)
    if strong:
        answers.mark_used(strong["ans_id"])
    return {"draft": text, "sources": sources}


async def humanize(draft: str, client: str) -> str:
    user = (
        f"Rewrite the passage below so it reads unmistakably as a senior human proposal "
        f"writer at iteria. Keep every fact and commitment. Stay concrete about {client}. "
        f"Return only the rewritten passage.\n\nPASSAGE:\n{draft}"
    )
    return await llm.complete(prompts.HUMANIZE_SYSTEM, user, cfg.polish_model,
                              max_tokens=1400, temperature=0.4)


async def shred_requirements(rfp_text: str) -> list[dict]:
    """Extract the requirements traceability matrix from a solicitation, verbatim."""
    if not rfp_text or len(rfp_text.strip()) < 40:
        return []

    extracted: list[dict] = []
    seen: set[str] = set()
    # Long solicitations exceed a single context comfortably. Window with overlap so
    # a requirement spanning a boundary is not lost.
    window, overlap = 14000, 1000
    for start in range(0, min(len(rfp_text), 120000), window - overlap):
        segment = rfp_text[start:start + window]
        if len(segment.strip()) < 200:
            continue
        try:
            batch = await llm.complete_json(
                prompts.RTM_SYSTEM,
                f"SOLICITATION (part {start // (window - overlap) + 1}):\n{segment}\n\n"
                "Return the requirements as a JSON array now.",
                expect=list, model=cfg.parse_model, max_tokens=4000,
            )
        except Exception as exc:
            log.warning("shred window at %s failed: %s", start, exc)
            continue

        for item in batch:
            if not isinstance(item, dict):
                continue
            text = (item.get("req_text") or "").strip()
            if len(text) < 12:
                continue
            key = text.lower()[:160]
            if key in seen:
                continue
            seen.add(key)
            module = str(item.get("module", "GENERAL")).upper()
            response_type = str(item.get("response_type", "narrative")).lower()
            extracted.append({
                "rfp_ref": str(item.get("rfp_ref", ""))[:120],
                "req_text": text,
                "module": module if module in classifier.MODULES else "GENERAL",
                "mandatory": "Y" if item.get("mandatory") else "N",
                "response_type": response_type
                if response_type in ("narrative", "questionnaire", "form", "pricing")
                else "narrative",
            })
    log.info("shred extracted %s requirements", len(extracted))
    return extracted


async def parse_rfp(rfp_text: str) -> dict:
    """Llama extracts structured fields; Cohere-style vector match finds past
    responses that fit what the solicitation asks for."""
    if not rfp_text or len(rfp_text.strip()) < 40:
        return {"parsed_fields": {}, "matches": []}
    fields = await llm.complete_json(
        prompts.RFP_PARSE_SYSTEM,
        f"SOLICITATION:\n{rfp_text[:14000]}\n\nReturn the JSON object now.",
        expect=dict, model=cfg.parse_model, max_tokens=1500,
    )
    probe_bits = []
    for key in ("client_name", "agency", "industry", "legacy_systems",
                "pain_points", "required_modules", "annual_budget"):
        value = fields.get(key)
        if isinstance(value, list):
            probe_bits.append(" ".join(str(v) for v in value if v))
        elif value:
            probe_bits.append(str(value))
    probe = " ".join(probe_bits).strip() or rfp_text[:1200]

    seen: set[str] = set()
    matches: list[dict] = []
    # Broad match on the solicitation ask, then module-specific probes so FIN /
    # HCM / PAYROLL past wins surface when those modules are requested.
    module_probes: list[tuple[str | None, str]] = [(None, probe)]
    modules = fields.get("required_modules") or []
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.replace(";", ",").split(",") if m.strip()]
    for module in modules[:6]:
        module_probes.append((str(module).upper()[:16], f"{probe} {module}"))

    for module, text in module_probes:
        for chunk in retrieval.retrieve(text, module if module and module in (
                "FIN", "HCM", "PAYROLL", "PROC", "BUDGET", "INV", "TECH", "GENERAL"
        ) else None, k=4):
            key = f"{chunk.get('doc_id')}:{chunk.get('module')}"
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "doc_id": chunk.get("doc_id"),
                "filename": chunk.get("filename"),
                "client": chunk.get("client"),
                "module": chunk.get("module"),
                "outcome": chunk.get("outcome"),
                "score": chunk.get("score"),
                "excerpt": (chunk.get("text") or "")[:280],
            })
            if len(matches) >= 10:
                break
        if len(matches) >= 10:
            break

    matches.sort(key=lambda m: m.get("score") or 99)
    return {"parsed_fields": fields, "matches": matches[:8]}


def _match_code(chosen: str, allowed: list[str]) -> str:
    if not allowed:
        return chosen
    if not chosen:
        return _preferred_constructive_code(allowed)
    lowered = chosen.strip().lower()
    for code in allowed:
        if code.strip().lower() == lowered:
            return code
    for code in allowed:
        if lowered in code.lower() or code.lower() in lowered:
            return code
    return _preferred_constructive_code(allowed)


def _is_negative_code(code: str) -> bool:
    c = (code or "").strip().lower()
    if not c:
        return False
    if c in {"no", "none", "n/a", "na"}:
        return True
    return any(h in c for h in _NEGATIVE_CODE_HINTS)


def _preferred_constructive_code(allowed: list[str]) -> str:
    """Prefer a positive capability code over trailing Not Available defaults."""
    if not allowed:
        return "Configuration"
    ranked = ("standard", "configuration", "config", "yes", "fully", "meets",
              "compliant", "supported", "modification", "third party", "partner")
    lowered = [(code, code.strip().lower()) for code in allowed]
    for hint in ranked:
        for code, low in lowered:
            if hint in low and not _is_negative_code(code):
                return code
    for code, _ in lowered:
        if not _is_negative_code(code):
            return code
    return allowed[0]


def _looks_like_cannot_do(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _CANNOT_DO_HINTS)


def _ensure_human_flag(text: str, reason: str) -> str:
    body = (text or "").strip()
    if "[NEEDS HUMAN" in body.upper():
        return body
    flag = f"[NEEDS HUMAN: {reason}]"
    return f"{flag} {body}".strip() if body else flag


async def answer_question(question: str, module: str | None = None,
                          allowed_codes: list[str] | None = None) -> dict:
    """Answer one questionnaire row. Library miss must still get a constructive
    draft; weak rows are flagged for human review instead of "cannot complete"."""
    codes = allowed_codes or DEFAULT_CODES
    context, sources, strong = _grounding(question, module)
    has_chunks = any(s["kind"] == "chunk" for s in sources)
    library_miss = not strong and not has_chunks

    user = (
        f"QUESTION:\n{question}\n\n"
        f"ALLOWED RESPONSE CODES: {', '.join(codes)}\n\n"
        f"ITERIA MATERIAL:\n{context}\n\n"
        "Answer now as JSON. Remember: a thin library is not a decline. "
        "Draft constructively from Oracle Fusion / iteria practice and mark "
        "only true unknowns with [NEEDS HUMAN: ...]."
    )
    result = await llm.complete_json(prompts.QA_SYSTEM, user, expect=dict,
                                     model=cfg.draft_model, max_tokens=900)

    code = _match_code(str(result.get("response_code", "")), codes)
    text = str(result.get("response_text", "")).strip()
    try:
        confidence = float(result.get("confidence", 0.4))
    except (TypeError, ValueError):
        confidence = 0.4
    confidence = max(0.0, min(1.0, confidence))
    needs_human = bool(result.get("needs_human")) or (HUMAN_FLAG in text)

    if strong:
        confidence = max(confidence, 0.82)
        answers.mark_used(strong["ans_id"])

    # Library miss / weak grounding: never leave a "we can't do it" answer.
    if library_miss or _looks_like_cannot_do(text) or (
        not strong and _is_negative_code(code)
    ):
        if _is_negative_code(code) and (library_miss or _looks_like_cannot_do(text)):
            code = _preferred_constructive_code(codes)
        if library_miss or _looks_like_cannot_do(text) or needs_human:
            text = _ensure_human_flag(
                text,
                "no approved library exemplar — verify this draft before submission",
            )
            needs_human = True
        if library_miss:
            confidence = min(confidence, 0.30)
        elif needs_human:
            confidence = min(confidence, 0.45)

    return {
        "response_code": code,
        "response_text": text,
        "confidence": round(confidence, 2),
        "source_answer_id": strong["ans_id"] if strong else None,
        "sources": sources,
        "needs_human": needs_human,
    }


async def chat(question: str) -> dict:
    context, sources, _ = _grounding(question, None)
    user = f"QUESTION: {question}\n\nITERIA MATERIAL:\n{context}\n\nAnswer now."
    text = await llm.complete(prompts.CHAT_SYSTEM, user, cfg.draft_model, max_tokens=1200)
    return {"answer": text, "sources": sources}


async def draft_section(client: str, title: str, module: str | None, brief: str) -> str:
    context, _, _ = _grounding(f"{title}. {brief}", module)
    user = (
        f"CLIENT: {client}\nSECTION: {title}\n"
        f"MODULE: {MODULE_TITLES.get(module, module) if module else 'general'}\n"
        f"CONTEXT: {brief}\n\n"
        f"ITERIA'S OWN MATERIAL (match this voice, reuse this substance, never copy verbatim):\n"
        f"{context}\n\n"
        "Write this section of iteria's proposal now. Two to four tight paragraphs, "
        "specific to this client."
    )
    return await llm.complete(prompts.DRAFT_SYSTEM, user, cfg.draft_model, max_tokens=1400)


async def draft_many(items: list[dict]) -> list[dict]:
    """Draft several requirements concurrently. llm.py bounds the real concurrency,
    so this saturates the allowance without stampeding the API."""
    async def one(item: dict) -> dict:
        try:
            result = await draft_requirement(
                item["req_text"], item.get("module_tag", "GENERAL"),
                item["client"], item.get("state"),
            )
            return {"req_id": item["req_id"], **result}
        except Exception as exc:
            log.warning("draft failed req_id=%s: %s", item.get("req_id"), exc)
            return {"req_id": item["req_id"], "error": str(exc)}

    return list(await asyncio.gather(*(one(item) for item in items)))
