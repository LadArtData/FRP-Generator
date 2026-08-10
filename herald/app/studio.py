"""FRP Studio contract adapter.

The Studio the team designed calls a window.FRP bridge whose vocabulary is
"proposals" with a draft_text, attachments, and parsed fields. HARALD's model is
the opportunity. This module maps one onto the other, so the Studio runs unchanged
against the same bid record that Bids & Compliance tracks and packages are built
from. Editing a bid in one workspace is visible in the other, because they are the
same row.
"""
from __future__ import annotations

import io
import json
import logging
import re

from docx import Document
from docx.shared import Pt

from . import audit, documents, generation, opportunities
from .db import clob, cursor, transaction
from .errors import NotFound

log = logging.getLogger("harald.studio")


def create_proposal(payload: dict, actor: str) -> dict:
    opp_id = opportunities.create(
        {"client_name": payload.get("client_name"),
         "title": payload.get("client_name") or "Untitled bid",
         "due_date": payload.get("due_date")},
        actor,
    )
    if payload.get("rfp_doc_id"):
        opportunities.update(opp_id, {"rfp_doc_id": payload["rfp_doc_id"]}, actor)
    return {"proposal_id": opp_id}


def list_proposals(limit: int = 100) -> list[dict]:
    return [
        {"proposal_id": o["opp_id"], "client_name": o["client_name"],
         "status": o["gen_status"] if o["gen_status"] == "generating" else o["status"],
         "updated_at": o["updated_at"]}
        for o in opportunities.list_all(limit)
    ]


def get_proposal(opp_id: int) -> dict:
    opp = opportunities.get(opp_id)
    return {
        "proposal_id": opp["opp_id"],
        "client_name": opp["client_name"],
        # The Studio polls status to know when generation finishes.
        "status": "generating" if opp["gen_status"] == "generating" else opp["status"],
        "rfp_doc_id": opp["rfp_doc_id"],
        "due_date": opp["due_date"],
        "draft_text": opp["draft_text"],
        "extracted_json": opp["extracted_json"],
        "gen_error": opp["gen_error"],
        "updated_at": opp["updated_at"],
        "attachments": [
            {"doc_id": d["doc_id"], "filename": d["filename"], "role": d["doc_role"],
             "deal_status": None, "attached_at": d["uploaded_at"]}
            for d in opp["documents"]
        ],
    }


def update_proposal(opp_id: int, payload: dict, actor: str) -> dict:
    mapped = {k: v for k, v in payload.items()
              if k in ("client_name", "due_date", "rfp_doc_id", "draft_text",
                       "status", "form_state", "parsed_fields")}
    opportunities.update(opp_id, mapped, actor)
    opp = opportunities.get(opp_id)
    return {"ok": True, "updated_at": opp["updated_at"]}


def attach(opp_id: int, doc_id: int, role: str | None, actor: str) -> dict:
    """The Studio attaches a library document to a bid. In the unified model a
    document belongs to the bid, so attaching binds it and sets its role."""
    documents.get(doc_id)
    with transaction() as conn:
        conn.cursor().execute(
            "UPDATE harald_documents SET opp_id = :opp, doc_role = :role WHERE doc_id = :d",
            {"opp": opp_id, "role": role or "reference", "d": doc_id},
        )
    if (role or "") == "rfp":
        opportunities.update(opp_id, {"rfp_doc_id": doc_id}, actor)
    audit.record(actor, "studio.attach", "opportunity", opp_id,
                 {"doc_id": doc_id, "role": role})
    return {"ok": True}


async def parse(doc_id: int) -> dict:
    """Autofill: read the solicitation, extract its fields, and persist them onto
    the bid so the Studio form and package assembly share the same understanding."""
    text = documents.get_text(doc_id)
    result = await generation.parse_rfp(text)
    doc = documents.get(doc_id)

    if doc.get("opp_id"):
        fields = result["parsed_fields"]
        opportunities.update(doc["opp_id"], {
            "parsed_fields": fields,
            **({"client_name": fields["client_name"]} if fields.get("client_name") else {}),
            **({"solicitation_no": fields["rfp_number"]} if fields.get("rfp_number") else {}),
            **({"due_date": fields["due_date"]} if fields.get("due_date") else {}),
            **({"agency": fields["agency"]} if fields.get("agency") else {}),
        }, None)

    return {"parsed_fields": result["parsed_fields"],
            "match_data": {"matches": result["matches"]},
            "filename": doc["filename"]}


async def generate(opp_id: int, actor: str) -> None:
    """The Studio's Generate. When the bid has a requirements matrix, draft against
    it so the Studio and the compliance view stay in step. With no matrix yet, draft
    the standard proposal sections so the Studio still produces a full narrative."""
    opp = opportunities.get(opp_id)
    if opp["requirements"]:
        await opportunities.generate_narrative(opp_id, actor)
        return

    try:
        opportunities.set_generation_state(opp_id, "generating")
        form: dict = {}
        try:
            extracted = json.loads(opp["extracted_json"] or "null") or {}
            form = {**(extracted.get("parsed_fields") or {}),
                    **(extracted.get("studio_form") or {})}
        except (json.JSONDecodeError, TypeError):
            form = {}

        client = opp["client_name"] or "the client"
        brief = ", ".join(
            f"{label} {form[key]}"
            for key, label in (("industry", "industry"), ("legacy_systems", "legacy systems"),
                               ("pain_points", "pain points"))
            if form.get(key)
        ) or "public-sector Oracle Cloud Fusion ERP modernization"

        modules = _modules_from(form)
        plan: list[tuple[str, str | None]] = [("Executive Summary", None)]
        plan += [(generation.MODULE_TITLES[m], m) for m in modules]
        plan += [("Implementation Approach", None),
                 ("Project Management and Governance", None),
                 ("Support and Managed Services", "TECH")]

        blocks: list[str] = []
        for title, module in plan:
            body = await generation.draft_section(client, title, module, brief)
            blocks.extend([title.upper(), "", body.strip(), ""])

        opportunities.update(opp_id, {"draft_text": "\n".join(blocks).strip()}, actor)
        opportunities.set_generation_state(opp_id, "idle")
        audit.record(actor, "studio.generate", "opportunity", opp_id,
                     {"sections": len(plan)})
    except Exception as exc:
        log.exception("studio generation failed opp=%s", opp_id)
        opportunities.set_generation_state(opp_id, "error", str(exc))


_MODULE_ALIASES = {
    "financials": "FIN", "finance": "FIN", "financial": "FIN", "gl": "FIN",
    "hr": "HCM", "human resources": "HCM", "hcm": "HCM",
    "payroll": "PAYROLL", "procurement": "PROC", "purchasing": "PROC",
    "budget": "BUDGET", "inventory": "INV", "assets": "INV",
    "technical": "TECH", "it": "TECH",
}


def _modules_from(form: dict) -> list[str]:
    raw = form.get("required_modules")
    if isinstance(raw, str):
        raw = [part for part in raw.replace(";", ",").split(",")]
    if not isinstance(raw, list):
        raw = []

    resolved: list[str] = []
    for entry in raw:
        key = str(entry).strip()
        upper = key.upper()
        if upper in generation.MODULE_TITLES and upper != "GENERAL":
            resolved.append(upper)
        elif key.lower() in _MODULE_ALIASES:
            resolved.append(_MODULE_ALIASES[key.lower()])

    ordered = list(dict.fromkeys(resolved))
    return ordered or ["FIN", "HCM", "PAYROLL", "PROC", "TECH"]


def _safe_filename(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "proposal").strip())
    return (cleaned.strip("._") or "proposal")[:80]


def _studio_form(opp: dict) -> dict:
    raw = opp.get("extracted_json")
    if not raw or raw == "null":
        return {}
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    form = data.get("studio_form") or data.get("form") or {}
    return form if isinstance(form, dict) else {}


def export_docx(opp_id: int) -> tuple[bytes, str]:
    """Build a Word document from the saved Studio draft for download."""
    opp = opportunities.get(opp_id)
    form = _studio_form(opp)
    client = (opp.get("client_name") or form.get("client_name") or "Proposal").strip()

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    document.add_heading(client, level=0)
    meta = document.add_paragraph()
    meta_bits = [f"Status: {opp.get('status') or 'draft'}"]
    if opp.get("due_date"):
        meta_bits.append(f"Due: {opp['due_date']}")
    if form.get("rfp_number"):
        meta_bits.append(f"Solicitation: {form['rfp_number']}")
    meta_bits.append(f"Proposal ID: {opp_id}")
    meta.add_run(" · ".join(meta_bits)).italic = True

    summary_keys = [
        ("industry", "Industry"),
        ("primary_contact", "Primary contact"),
        ("annual_budget", "Annual budget / revenue"),
        ("legacy_systems", "Current ERP / systems"),
        ("engagement_type", "Engagement type"),
        ("primary_competition", "Primary competition"),
        ("win_theme", "Win theme"),
        ("project_manager", "Project manager"),
        ("solution_architect", "Solution architect"),
    ]
    summary_rows = []
    for key, label in summary_keys:
        value = form.get(key)
        if value:
            summary_rows.append((label, str(value)))
    for key, label in (("pain_points", "Pain points"), ("proposed_modules", "Proposed modules")):
        value = form.get(key)
        if isinstance(value, list) and value:
            summary_rows.append((label, ", ".join(str(v) for v in value)))
        elif value:
            summary_rows.append((label, str(value)))

    if summary_rows:
        document.add_heading("Proposal inputs", level=1)
        for label, value in summary_rows:
            paragraph = document.add_paragraph()
            paragraph.add_run(f"{label}: ").bold = True
            paragraph.add_run(value)

    draft = (opp.get("draft_text") or "").strip()
    document.add_heading("Draft", level=1)
    if not draft:
        document.add_paragraph("No draft text yet. Run Generate, then export again.")
    else:
        for block in re.split(r"\n\s*\n", draft):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            first = lines[0].strip()
            if first.startswith("#"):
                level = min(len(first) - len(first.lstrip("#")), 3)
                title = first.lstrip("#").strip() or "Section"
                document.add_heading(title, level=level)
                body = "\n".join(lines[1:]).strip()
                if body:
                    document.add_paragraph(body)
            else:
                document.add_paragraph(block)

    buffer = io.BytesIO()
    document.save(buffer)
    filename = f"{_safe_filename(client)}.docx"
    return buffer.getvalue(), filename
