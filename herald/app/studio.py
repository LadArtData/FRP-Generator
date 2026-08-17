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
import zipfile

from docx import Document
from docx.shared import Pt

from . import (audit, documents, engagement, field_mapping, generation,
               iteria_capabilities, opportunities, packages, pricing_matrix,
               questionnaires)
from .db import cursor, transaction
from .errors import NotFound, ValidationFailed

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


def list_proposals(limit: int = 100, *, include_empty: bool = False) -> list[dict]:
    rows: list[dict] = []
    for o in opportunities.list_all(limit):
        draft_chars = int(o.get("draft_chars") or 0)
        demo = opportunities.is_demo_or_blank(o, draft_chars=draft_chars)
        if demo and not include_empty:
            continue
        gen = o.get("gen_status") or "idle"
        rows.append({
            "proposal_id": o["opp_id"],
            "client_name": o["client_name"],
            "agency": o.get("agency"),
            "solicitation_no": o.get("solicitation_no"),
            "due_date": o.get("due_date"),
            "display_label": opportunities.display_label(o),
            "status": gen if gen == "generating" else o["status"],
            "updated_at": o["updated_at"],
            "attachment_count": o.get("doc_count", 0),
            "draft_chars": draft_chars,
            "is_demo": demo,
        })
    return rows


def delete_proposal(opp_id: int, actor: str) -> dict:
    opportunities.delete(opp_id, actor)
    return {"ok": True, "proposal_id": opp_id}


def cleanup_demos(actor: str) -> dict:
    return opportunities.cleanup_workspace(actor)


def get_proposal(opp_id: int) -> dict:
    opp = opportunities.get(opp_id)
    gen = opp.get("gen_status") or "idle"
    if gen == "generating":
        status = "generating"
    elif gen == "error":
        status = "error"
    else:
        status = opp["status"]
    label = opportunities.display_label({
        "client_name": opp["client_name"], "agency": opp.get("agency"),
        "solicitation_no": opp.get("solicitation_no"), "due_date": opp.get("due_date"),
    })
    draft_chars = len(opp.get("draft_text") or "")
    return {
        "proposal_id": opp["opp_id"],
        "client_name": opp["client_name"],
        "display_label": label,
        "agency": opp.get("agency"),
        "solicitation_no": opp.get("solicitation_no"),
        "due_date": opp.get("due_date"),
        "draft_chars": draft_chars,
        "is_demo": opportunities.is_demo_or_blank(
            {"client_name": opp["client_name"], "doc_count": len(opp.get("documents") or []),
             "req_count": len(opp.get("requirements") or [])},
            draft_chars=draft_chars,
        ),
        "status": status,
        "gen_status": gen,
        "rfp_doc_id": opp["rfp_doc_id"],
        "draft_text": opp["draft_text"],
        "extracted_json": opp["extracted_json"],
        "gen_error": opp["gen_error"],
        "updated_at": opp["updated_at"],
        "attachments": [
            {"doc_id": d["doc_id"], "filename": d["filename"], "role": d["doc_role"],
             "deal_status": None, "attached_at": d["uploaded_at"]}
            for d in opp["documents"]
        ],
        "questionnaires": questionnaires.list_for_opportunity(opp_id),
        "match_data": _match_data(opp),
    }


def update_proposal(opp_id: int, payload: dict, actor: str) -> dict:
    mapped = {k: v for k, v in payload.items()
              if k in ("client_name", "due_date", "rfp_doc_id", "draft_text",
                       "status", "form_state", "parsed_fields", "match_data")}
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
    the bid so the Studio form and package assembly share the same understanding.

    The form is written here, on the server, rather than left to whatever the
    browser happens to scrape off the DOM. Every consumer merges ``studio_form``
    over ``parsed_fields`` (``opportunities.grounding_context``,
    ``pricing_matrix._context_from_opp``), so a parse that updates only
    ``parsed_fields`` is a parse that changes nothing downstream: the stale form
    still wins. Reconciling both halves in one transaction is what makes the
    button do what its label says.
    """
    text = documents.get_text(doc_id)
    result = await generation.parse_rfp(text)
    doc = documents.get(doc_id)

    fields = result["parsed_fields"]
    match_data = {"matches": result["matches"]}
    changed: dict = {}
    conflicts: list[dict] = []
    form: dict = {}

    if doc.get("opp_id"):
        opp = opportunities.get(doc["opp_id"])
        form, changed, conflicts = field_mapping.reconcile(
            _studio_form(opp), fields, rfp_text=text,
        )
        # due_date is stored ISO on the row as well, so the bid list and the
        # form cannot disagree about the deadline.
        iso_due = form.get("due_date") or ""
        opportunities.update(doc["opp_id"], {
            "parsed_fields": fields,
            "form_state": form,
            "match_data": match_data,
            **({"client_name": fields["client_name"]} if fields.get("client_name") else {}),
            **({"solicitation_no": fields["rfp_number"]} if fields.get("rfp_number") else {}),
            **({"due_date": iso_due} if iso_due else {}),
            **({"agency": fields["agency"]} if fields.get("agency") else {}),
        }, None)
        if conflicts:
            log.info("autofill conflicts opp=%s: %s",
                     doc["opp_id"], [c["field"] for c in conflicts])

    return {"parsed_fields": fields,
            "form_state": form,
            "changed_fields": changed,
            "conflicts": conflicts,
            "match_data": match_data,
            "filename": doc["filename"]}


async def generate(opp_id: int, actor: str) -> None:
    """Draft the narrative and fill any attached agency Excel questionnaires.

    Spreadsheet fill writes into the agency's own workbook format (dropdowns and
    validations preserved) so download can ship the completed matrix with the
    Word draft and supporting attachments.
    """
    try:
        opportunities.set_generation_state(opp_id, "generating")
        opp = opportunities.get(opp_id)
        if opp["requirements"]:
            await opportunities.generate_narrative(opp_id, actor)
            # generate_narrative clears gen_status; keep the Studio poller busy
            # while spreadsheets fill.
            opportunities.set_generation_state(opp_id, "generating")
        else:
            await _draft_sections(opp_id, actor, opp)

        filled = await _fill_attached_spreadsheets(opp_id, actor)
        opportunities.set_generation_state(opp_id, "idle")
        audit.record(actor, "studio.generate", "opportunity", opp_id,
                     {"spreadsheets_filled": filled})
    except Exception as exc:
        log.exception("studio generation failed opp=%s", opp_id)
        opportunities.set_generation_state(opp_id, "error", str(exc))


async def _draft_sections(opp_id: int, actor: str, opp: dict) -> None:
    rfp_text, form = opportunities.grounding_context(opp)
    profile = engagement.classify_opportunity(form, rfp_text)
    client = opp["client_name"] or "the client"
    pain = form.get("pain_points")
    if isinstance(pain, list):
        pain = ", ".join(str(p) for p in pain)
    # The checkbox list is a closed vocabulary of seven ERP complaints. When the
    # solicitation states a need it cannot express — "AI adoption and
    # enablement" — the free text carries it instead of it being dropped.
    pain_text = str(form.get("pain_points_text") or "").strip()
    if pain_text and pain_text.lower() not in (pain or "").lower():
        pain = f"{pain}, {pain_text}" if pain else pain_text
    brief_parts = [f"engagement profile {profile.label}"]
    for key, label, value in (
        ("industry", "industry", form.get("industry")),
        ("legacy_systems", "legacy systems", form.get("legacy_systems")),
        ("pain_points", "pain points", pain),
        ("win_theme", "win theme", form.get("win_theme")),
        ("engagement_type", "engagement", form.get("engagement_type")),
    ):
        if value:
            brief_parts.append(f"{label} {value}")
    brief = ", ".join(brief_parts)

    plan = iteria_capabilities.section_plan(profile)
    blocks: list[str] = []
    for title, module in plan:
        body = await generation.draft_section(
            client, title, module, brief,
            rfp_text=rfp_text, parsed_fields=form,
        )
        blocks.extend([title.upper(), "", body.strip(), ""])

    opportunities.update(opp_id, {"draft_text": "\n".join(blocks).strip()}, actor)


async def _fill_attached_spreadsheets(opp_id: int, actor: str) -> int:
    """Import and fill .xlsx/.xlsm attachments so agency matrices are completed."""
    opp = opportunities.get(opp_id)
    already = {}
    for q in questionnaires.list_for_opportunity(opp_id):
        detail = questionnaires.get(q["q_id"])
        already[detail["source_doc_id"]] = q["q_id"]

    filled = 0
    for doc in opp.get("documents") or []:
        name = (doc.get("filename") or "").lower()
        if not name.endswith((".xlsx", ".xlsm")):
            continue
        q_id = already.get(doc["doc_id"])
        if q_id is None:
            try:
                imported = questionnaires.import_workbook(opp_id, doc["doc_id"], actor)
                q_id = imported["q_id"]
            except ValidationFailed as exc:
                log.warning("skip spreadsheet import doc=%s: %s", doc["doc_id"], exc)
                continue
            except Exception:
                log.exception("spreadsheet import failed doc=%s", doc["doc_id"])
                continue
        try:
            await questionnaires.fill(q_id, actor)
            filled += 1
        except Exception:
            log.exception("spreadsheet fill failed q_id=%s", q_id)
    return filled


_MODULE_ALIASES = {
    "financials": "FIN", "finance": "FIN", "financial": "FIN", "gl": "FIN",
    "hr": "HCM", "human resources": "HCM", "hcm": "HCM",
    "payroll": "PAYROLL", "procurement": "PROC", "purchasing": "PROC",
    "budget": "BUDGET", "inventory": "INV", "assets": "INV",
    "technical": "TECH", "it": "TECH",
    "epm": "BUDGET", "scm": "INV", "analytics / oac": "TECH",
    "analytics/oac": "TECH", "project portfolio": "TECH",
}


def _modules_from(form: dict, profile=None) -> list[str]:
    raw = form.get("proposed_modules") or form.get("required_modules")
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
    if ordered:
        return ordered
    if profile is not None:
        return engagement.default_modules(profile)
    return ["FIN", "HCM", "PAYROLL", "PROC", "TECH"]


def _safe_filename(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "proposal").strip())
    return (cleaned.strip("._") or "proposal")[:80]


def _extracted(opp: dict) -> dict:
    raw = opp.get("extracted_json")
    if not raw or raw == "null":
        return {}
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _studio_form(opp: dict) -> dict:
    data = _extracted(opp)
    form = data.get("studio_form") or data.get("form") or {}
    return form if isinstance(form, dict) else {}


def _match_data(opp: dict) -> dict:
    data = _extracted(opp)
    match = data.get("match_data") or {}
    if isinstance(match, dict) and isinstance(match.get("matches"), list):
        return match
    return {"matches": []}


async def assemble_package(opp_id: int, actor: str) -> dict:
    """Build the agency-format submission package from the Studio bid."""
    return await packages.assemble(opp_id, actor)


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


def export_materials_zip(opp_id: int) -> tuple[bytes, str]:
    """Zip the Word draft, filled agency Excels, attachments, and package files."""
    opp = opportunities.get(opp_id)
    client = _safe_filename(opp.get("client_name") or "proposal")
    buffer = io.BytesIO()
    used_names: set[str] = set()

    def _unique(name: str) -> str:
        base = name or "file.bin"
        if base not in used_names:
            used_names.add(base)
            return base
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        index = 2
        while True:
            candidate = f"{stem}_{index}.{ext}" if ext else f"{stem}_{index}"
            if candidate not in used_names:
                used_names.add(candidate)
                return candidate
            index += 1

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        docx_bytes, docx_name = export_docx(opp_id)
        archive.writestr(f"01_narrative/{_unique(docx_name)}", docx_bytes)

        for q in questionnaires.list_for_opportunity(opp_id):
            try:
                xlsx_bytes, xlsx_name = questionnaires.export(q["q_id"])
                archive.writestr(
                    f"02_filled_spreadsheets/{_unique(xlsx_name)}", xlsx_bytes
                )
            except Exception:
                log.exception("materials zip: questionnaire export failed q=%s", q["q_id"])

        for doc in opp.get("documents") or []:
            try:
                blob, filename = documents.get_blob(doc["doc_id"])
                fallback = "doc_" + str(doc["doc_id"])
                archive.writestr(
                    "03_attachments/" + _unique(filename or fallback),
                    blob,
                )
            except Exception:
                log.exception("materials zip: attachment failed doc=%s", doc["doc_id"])

        for pkg in packages.list_for_opportunity(opp_id):
            pkg_id = pkg["package_id"]
            try:
                blob, filename, _ = packages.download(pkg_id, "docx")
                fallback = "package_" + str(pkg_id) + ".docx"
                archive.writestr(
                    "04_packages/" + _unique(filename or fallback),
                    blob,
                )
            except Exception:
                log.exception("materials zip: package docx failed id=%s", pkg_id)
            if pkg.get("has_pdf"):
                try:
                    blob, filename, _ = packages.download(pkg_id, "pdf")
                    fallback = "package_" + str(pkg_id) + ".pdf"
                    archive.writestr(
                        "04_packages/" + _unique(filename or fallback),
                        blob,
                    )
                except Exception:
                    log.exception("materials zip: package pdf failed id=%s", pkg_id)

        try:
            csv_text = pricing_matrix.to_csv(opp_id)
            archive.writestr(
                "05_pricing/" + _unique(client + "_pricing_matrix.csv"),
                csv_text.encode("utf-8"),
            )
        except Exception:
            log.exception("materials zip: pricing matrix failed opp=%s", opp_id)

        manifest = {
            "proposal_id": opp_id,
            "client_name": opp.get("client_name"),
            "status": opp.get("status"),
            "files": sorted(used_names),
        }
        archive.writestr("README.json", json.dumps(manifest, indent=2))

    return buffer.getvalue(), f"{client}_materials.zip"
