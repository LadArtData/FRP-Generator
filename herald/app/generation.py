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


def _exemplars(chunks: list[dict]) -> str:
    if not chunks:
        return ("[no close match in iteria's library. Write from iteria's general "
                "public-sector Oracle Fusion approach and mark any client-specific "
                "claim in [BRACKETS] for a consultant to supply.]")
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
                expect=list, model=cfg.draft_model, max_tokens=4000,
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
    if not rfp_text or len(rfp_text.strip()) < 40:
        return {"parsed_fields": {}, "matches": []}
    fields = await llm.complete_json(
        prompts.RFP_PARSE_SYSTEM,
        f"SOLICITATION:\n{rfp_text[:14000]}\n\nReturn the JSON object now.",
        expect=dict, model=cfg.draft_model, max_tokens=1500,
    )
    probe = " ".join(
        str(fields.get(key, "")) for key in ("pain_points", "legacy_systems", "industry")
    ) or rfp_text[:1200]
    matches = [
        {"client": c["client"], "module": c["module"], "outcome": c["outcome"],
         "score": c["score"]}
        for c in retrieval.retrieve(probe, None, k=6)
    ]
    return {"parsed_fields": fields, "matches": matches}


def _match_code(chosen: str, allowed: list[str]) -> str:
    if not allowed:
        return chosen
    if not chosen:
        return allowed[-1]
    lowered = chosen.strip().lower()
    for code in allowed:
        if code.strip().lower() == lowered:
            return code
    for code in allowed:
        if lowered in code.lower() or code.lower() in lowered:
            return code
    return allowed[-1]


async def answer_question(question: str, module: str | None = None,
                          allowed_codes: list[str] | None = None) -> dict:
    """Answer one questionnaire row. Confidence reflects how well iteria's own
    material actually supports the answer, so weak rows route to a human."""
    codes = allowed_codes or DEFAULT_CODES
    context, sources, strong = _grounding(question, module)
    has_chunks = any(s["kind"] == "chunk" for s in sources)

    user = (
        f"QUESTION:\n{question}\n\n"
        f"ALLOWED RESPONSE CODES: {', '.join(codes)}\n\n"
        f"ITERIA MATERIAL:\n{context}\n\n"
        "Answer now as JSON."
    )
    result = await llm.complete_json(prompts.QA_SYSTEM, user, expect=dict,
                                     model=cfg.draft_model, max_tokens=900)

    code = _match_code(str(result.get("response_code", "")), codes)
    try:
        confidence = float(result.get("confidence", 0.4))
    except (TypeError, ValueError):
        confidence = 0.4
    confidence = max(0.0, min(1.0, confidence))

    if strong:
        confidence = max(confidence, 0.82)
        answers.mark_used(strong["ans_id"])
    if not strong and not has_chunks:
        confidence = min(confidence, 0.30)

    return {
        "response_code": code,
        "response_text": str(result.get("response_text", "")).strip(),
        "confidence": round(confidence, 2),
        "source_answer_id": strong["ans_id"] if strong else None,
        "sources": sources,
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
