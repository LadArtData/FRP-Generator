"""The bid.

One entity. The Studio drafts against it, Bids & Compliance tracks it, packages
are assembled from it. Amendments load as superseding document versions and their
new requirements are flagged, so a late change updates the matrix instead of
forcing a manual re-shred.
"""
from __future__ import annotations

import json
import logging

from . import audit, documents, generation
from .db import clob, cursor, transaction
from .errors import NotFound, ValidationFailed

log = logging.getLogger("harald.opportunities")

STATUSES = ("evaluating", "bidding", "submitted", "won", "lost", "no_bid")
REQ_STATUSES = ("not_started", "in_progress", "drafted", "reviewed", "complete", "gap")


def create(payload: dict, actor: str | None = None) -> int:
    status = payload.get("status", "evaluating")
    if status not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")
    with transaction() as conn:
        cur = conn.cursor()
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_opportunities
                 (client_name, agency, solicitation_no, title, due_date, status,
                  bid_decision, portal_url, format_profile_id, created_by)
               VALUES (:client, :agency, :sol, :title, :due, :status, :decision,
                       :portal, :profile, :actor)
               RETURNING opp_id INTO :out""",
            {"client": payload.get("client_name") or "Untitled client",
             "agency": payload.get("agency"),
             "sol": payload.get("solicitation_no"),
             "title": payload.get("title") or payload.get("client_name") or "Untitled bid",
             "due": payload.get("due_date"), "status": status,
             "decision": payload.get("bid_decision"), "portal": payload.get("portal_url"),
             "profile": payload.get("format_profile_id"), "actor": actor, "out": out},
        )
        opp_id = out.getvalue()[0]
    audit.record(actor, "opportunity.create", "opportunity", opp_id,
                 {"client": payload.get("client_name")})
    return opp_id


def update(opp_id: int, payload: dict, actor: str | None = None) -> None:
    mapping = {
        "client_name": "client_name", "agency": "agency",
        "solicitation_no": "solicitation_no", "title": "title", "due_date": "due_date",
        "status": "status", "bid_decision": "bid_decision", "portal_url": "portal_url",
        "format_profile_id": "format_profile_id", "rfp_doc_id": "rfp_doc_id",
        "draft_text": "draft_text",
    }
    columns = {mapping[k]: v for k, v in payload.items() if k in mapping}
    if "status" in columns and columns["status"] not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")

    # form_state / parsed_fields / match_data merge into extracted_json
    if "form_state" in payload or "parsed_fields" in payload or "match_data" in payload:
        with cursor() as cur:
            cur.execute("SELECT extracted_json FROM harald_opportunities WHERE opp_id = :o",
                        {"o": opp_id})
            row = cur.fetchone()
        current: dict = {}
        if row and row[0]:
            try:
                current = json.loads(clob(row[0])) or {}
            except (json.JSONDecodeError, TypeError):
                current = {}
        if "form_state" in payload:
            current["studio_form"] = payload["form_state"]
        if "parsed_fields" in payload:
            current["parsed_fields"] = payload["parsed_fields"]
        if "match_data" in payload:
            current["match_data"] = payload["match_data"]
        columns["extracted_json"] = json.dumps(current)

    if not columns:
        return

    with transaction() as conn:
        cur = conn.cursor()
        assignments = ", ".join(f"{col} = :{col}" for col in columns)
        cur.execute(
            f"UPDATE harald_opportunities SET {assignments}, updated_at = SYSTIMESTAMP "
            f"WHERE opp_id = :opp_id",
            {**columns, "opp_id": opp_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Opportunity {opp_id} not found.")
    audit.record(actor, "opportunity.update", "opportunity", opp_id,
                 {"fields": list(columns)})


def set_generation_state(opp_id: int, state: str, error: str | None = None) -> None:
    with transaction() as conn:
        conn.cursor().execute(
            "UPDATE harald_opportunities SET gen_status = :s, gen_error = :e, "
            "updated_at = SYSTIMESTAMP WHERE opp_id = :o",
            {"s": state, "e": (error or "")[:2000] or None, "o": opp_id},
        )


def list_all(limit: int = 200) -> list[dict]:
    sql = """SELECT * FROM (
               SELECT o.opp_id, o.client_name, o.agency, o.solicitation_no, o.title,
                      o.due_date, o.status, o.gen_status, o.updated_at,
                      (SELECT COUNT(*) FROM harald_documents d WHERE d.opp_id = o.opp_id),
                      (SELECT COUNT(*) FROM harald_requirements r WHERE r.opp_id = o.opp_id),
                      (SELECT COUNT(*) FROM harald_requirements r
                        WHERE r.opp_id = o.opp_id AND r.status = 'complete'),
                      (SELECT COUNT(*) FROM harald_packages p WHERE p.opp_id = o.opp_id)
               FROM harald_opportunities o ORDER BY o.updated_at DESC
             ) WHERE ROWNUM <= :lim"""
    with cursor() as cur:
        cur.execute(sql, {"lim": limit})
        return [
            {"opp_id": r[0], "client_name": r[1], "agency": r[2], "solicitation_no": r[3],
             "title": r[4], "due_date": r[5], "status": r[6], "gen_status": r[7],
             "updated_at": r[8].isoformat() if r[8] else None,
             "doc_count": r[9], "req_count": r[10], "req_complete": r[11],
             "package_count": r[12]}
            for r in cur.fetchall()
        ]


def get(opp_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT opp_id, client_name, agency, solicitation_no, title, due_date, status,
                      bid_decision, portal_url, format_profile_id, rfp_doc_id, draft_text,
                      extracted_json, gen_status, gen_error, updated_at
               FROM harald_opportunities WHERE opp_id = :o""",
            {"o": opp_id},
        )
        row = cur.fetchone()
        if not row:
            raise NotFound(f"Opportunity {opp_id} not found.")
        opp = {
            "opp_id": row[0], "client_name": row[1], "agency": row[2],
            "solicitation_no": row[3], "title": row[4], "due_date": row[5], "status": row[6],
            "bid_decision": row[7], "portal_url": row[8], "format_profile_id": row[9],
            "rfp_doc_id": row[10], "draft_text": clob(row[11]),
            "extracted_json": clob(row[12]) or "null", "gen_status": row[13],
            "gen_error": row[14], "updated_at": row[15].isoformat() if row[15] else None,
            "documents": [], "requirements": [],
        }
        cur.execute(
            """SELECT doc_id, filename, doc_role, doc_class, version, effective_date,
                      supersedes_id, size_bytes, promoted_to_lib, uploaded_at
               FROM harald_documents WHERE opp_id = :o
               ORDER BY DECODE(doc_role,'rfp',1,'addendum',2,'questionnaire',3,
                               'cost_workbook',4,'form',5,6), uploaded_at""",
            {"o": opp_id},
        )
        opp["documents"] = [
            {"doc_id": r[0], "filename": r[1], "doc_role": r[2], "doc_class": r[3],
             "version": r[4], "effective_date": r[5], "supersedes_id": r[6],
             "size_bytes": r[7], "promoted_to_lib": r[8],
             "uploaded_at": r[9].isoformat() if r[9] else None}
            for r in cur.fetchall()
        ]
        cur.execute(
            """SELECT r.req_id, r.source_doc_id, r.rfp_ref, r.req_text, r.module_tag,
                      r.mandatory, r.response_type, r.section_ref, r.owner, r.status,
                      r.from_amendment, r.notes, r.sort_order,
                      NVL(d.final_text, d.draft_text), d.sources_json
               FROM harald_requirements r
               LEFT JOIN harald_drafts d ON d.req_id = r.req_id
               WHERE r.opp_id = :o ORDER BY r.sort_order, r.req_id""",
            {"o": opp_id},
        )
        for r in cur.fetchall():
            try:
                sources = json.loads(clob(r[14])) if r[14] else []
            except (json.JSONDecodeError, TypeError):
                sources = []
            opp["requirements"].append({
                "req_id": r[0], "source_doc_id": r[1], "rfp_ref": r[2],
                "req_text": clob(r[3]), "module": r[4], "mandatory": r[5],
                "response_type": r[6], "section_ref": r[7], "owner": r[8], "status": r[9],
                "from_amendment": r[10], "notes": r[11], "sort_order": r[12],
                "draft": clob(r[13]), "sources": sources,
            })
    return opp


def grounding_context(opp: dict) -> tuple[str, dict]:
    """RFP text + parsed fields for enterprise grounding on matrix and narrative fills."""
    parsed_fields: dict = {}
    try:
        extracted = json.loads(opp.get("extracted_json") or "null") or {}
        if isinstance(extracted, dict):
            parsed_fields = {
                **(extracted.get("parsed_fields") or {}),
                **(extracted.get("studio_form") or {}),
            }
    except (json.JSONDecodeError, TypeError):
        parsed_fields = {}

    rfp_text = ""
    if opp.get("rfp_doc_id"):
        try:
            rfp_text = documents.get_text(opp["rfp_doc_id"])
        except Exception:
            log.debug("grounding_context: no rfp text for opp=%s", opp.get("opp_id"))
    return rfp_text, parsed_fields


def compliance(opp_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM harald_requirements WHERE opp_id = :o GROUP BY status",
            {"o": opp_id},
        )
        by_status = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute(
            """SELECT module_tag, COUNT(*), SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END)
               FROM harald_requirements WHERE opp_id = :o GROUP BY module_tag
               ORDER BY module_tag""",
            {"o": opp_id},
        )
        by_module = [{"module": r[0], "total": r[1], "complete": int(r[2] or 0)}
                     for r in cur.fetchall()]
        cur.execute(
            """SELECT COUNT(*) FROM harald_requirements
               WHERE opp_id = :o AND mandatory = 'Y' AND status <> 'complete'""",
            {"o": opp_id},
        )
        mandatory_gaps = cur.fetchone()[0]
        cur.execute(
            """SELECT COUNT(*) FROM harald_requirements r
               LEFT JOIN harald_drafts d ON d.req_id = r.req_id
               WHERE r.opp_id = :o AND r.response_type = 'narrative'
                 AND NVL(DBMS_LOB.GETLENGTH(NVL(d.final_text, d.draft_text)), 0) = 0""",
            {"o": opp_id},
        )
        undrafted = cur.fetchone()[0]

    total = sum(by_status.values())
    complete = by_status.get("complete", 0)
    return {
        "total": total, "complete": complete, "by_status": by_status,
        "by_module": by_module, "mandatory_gaps": mandatory_gaps,
        "undrafted_narrative": undrafted,
        "percent_complete": round(100 * complete / total) if total else 0,
        "submittable": total > 0 and mandatory_gaps == 0,
    }


def add_requirements(opp_id: int, reqs: list[dict], source_doc_id: int | None = None,
                     from_amendment: str = "N", actor: str | None = None) -> int:
    if not reqs:
        return 0
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT NVL(MAX(sort_order), 0) FROM harald_requirements WHERE opp_id = :o",
            {"o": opp_id},
        )
        base = cur.fetchone()[0]
        cur.executemany(
            """INSERT INTO harald_requirements
                 (opp_id, source_doc_id, rfp_ref, req_text, module_tag, mandatory,
                  response_type, from_amendment, sort_order)
               VALUES (:opp, :src, :ref, :text, :mod, :mand, :rtype, :amend, :ord)""",
            [
                {"opp": opp_id, "src": source_doc_id, "ref": r.get("rfp_ref"),
                 "text": r["req_text"], "mod": r.get("module", "GENERAL"),
                 "mand": r.get("mandatory", "N"),
                 "rtype": r.get("response_type", "narrative"),
                 "amend": from_amendment, "ord": base + i + 1}
                for i, r in enumerate(reqs)
            ],
        )
        cur.execute(
            "UPDATE harald_opportunities SET status = DECODE(status, 'evaluating', 'bidding', status), "
            "updated_at = SYSTIMESTAMP WHERE opp_id = :o",
            {"o": opp_id},
        )
    audit.record(actor, "requirements.add", "opportunity", opp_id,
                 {"count": len(reqs), "amendment": from_amendment == "Y"})
    return len(reqs)


def update_requirement(req_id: int, payload: dict, actor: str | None = None) -> None:
    mapping = {"rfp_ref": "rfp_ref", "req_text": "req_text", "module": "module_tag",
               "mandatory": "mandatory", "response_type": "response_type",
               "section_ref": "section_ref", "owner": "owner", "status": "status",
               "notes": "notes"}
    columns = {mapping[k]: v for k, v in payload.items() if k in mapping}
    if "status" in columns and columns["status"] not in REQ_STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(REQ_STATUSES)}.")
    if not columns:
        return
    with transaction() as conn:
        cur = conn.cursor()
        assignments = ", ".join(f"{col} = :{col}" for col in columns)
        cur.execute(
            f"UPDATE harald_requirements SET {assignments} WHERE req_id = :req_id",
            {**columns, "req_id": req_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Requirement {req_id} not found.")
    audit.record(actor, "requirement.update", "requirement", req_id, {"fields": list(columns)})


def requirement(req_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT r.req_id, r.opp_id, r.module_tag, r.req_text, r.status,
                      o.client_name, o.agency, NVL(d.final_text, d.draft_text)
               FROM harald_requirements r
               JOIN harald_opportunities o ON o.opp_id = r.opp_id
               LEFT JOIN harald_drafts d ON d.req_id = r.req_id
               WHERE r.req_id = :r""",
            {"r": req_id},
        )
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Requirement {req_id} not found.")
    return {"req_id": row[0], "opp_id": row[1], "module_tag": row[2],
            "req_text": clob(row[3]), "status": row[4], "client": row[5],
            "agency": row[6], "draft": clob(row[7])}


def save_draft(req_id: int, draft: str | None, sources: list | None = None,
               final: str | None = None) -> None:
    """Upsert a draft. Writing a draft sets status drafted; a humanize pass sets
    reviewed. A requirement already marked complete is not downgraded."""
    payload = json.dumps(sources or [])
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT draft_id FROM harald_drafts WHERE req_id = :r", {"r": req_id})
        exists = cur.fetchone()
        if exists:
            if draft is not None and final is not None:
                cur.execute(
                    "UPDATE harald_drafts SET draft_text = :d, final_text = :f, "
                    "sources_json = :s, updated_at = SYSTIMESTAMP WHERE req_id = :r",
                    {"d": draft, "f": final, "s": payload, "r": req_id},
                )
            elif final is not None:
                cur.execute(
                    "UPDATE harald_drafts SET final_text = :f, updated_at = SYSTIMESTAMP "
                    "WHERE req_id = :r",
                    {"f": final, "r": req_id},
                )
            else:
                cur.execute(
                    "UPDATE harald_drafts SET draft_text = :d, sources_json = :s, "
                    "final_text = NULL, updated_at = SYSTIMESTAMP WHERE req_id = :r",
                    {"d": draft, "s": payload, "r": req_id},
                )
        else:
            cur.execute(
                "INSERT INTO harald_drafts (req_id, draft_text, final_text, sources_json) "
                "VALUES (:r, :d, :f, :s)",
                {"r": req_id, "d": draft, "f": final, "s": payload},
            )
        cur.execute(
            "UPDATE harald_requirements SET status = :s WHERE req_id = :r "
            "AND status NOT IN ('complete', 'reviewed')",
            {"s": "reviewed" if final is not None else "drafted", "r": req_id},
        )


async def generate_narrative(opp_id: int, actor: str | None = None) -> None:
    """Draft every undrafted narrative requirement, concurrently, then assemble a
    working narrative into the Studio's draft pane."""
    try:
        set_generation_state(opp_id, "generating")
        opp = get(opp_id)
        rfp_text, parsed_fields = grounding_context(opp)
        pending = [
            {"req_id": r["req_id"], "req_text": r["req_text"],
             "module_tag": r["module"], "client": opp["client_name"], "state": None,
             "rfp_text": rfp_text, "parsed_fields": parsed_fields}
            for r in opp["requirements"]
            if r["response_type"] == "narrative" and not (r["draft"] or "").strip()
        ]
        if pending:
            results = await generation.draft_many(pending)
            errors = [r for r in results if "error" in r]
            saved = 0
            for result in results:
                if "error" in result:
                    log.warning(
                        "draft failed req_id=%s: %s",
                        result.get("req_id"), result.get("error"),
                    )
                    continue
                try:
                    save_draft(
                        result["req_id"],
                        result.get("draft"),
                        result.get("sources"),
                        final=result.get("final"),
                    )
                    saved += 1
                except Exception as exc:
                    log.exception("save_draft failed req_id=%s", result.get("req_id"))
                    errors.append({"req_id": result.get("req_id"), "error": str(exc)})
            if pending and saved == 0:
                hint = (errors[0].get("error") if errors else "unknown error")
                set_generation_state(
                    opp_id, "error",
                    f"All {len(pending)} narrative drafts failed. First error: {hint}",
                )
                return

        refreshed = get(opp_id)
        blocks: list[str] = []
        for module in generation.MODULE_TITLES:
            drafted = [r for r in refreshed["requirements"]
                       if r["module"] == module and (r["draft"] or "").strip()]
            if not drafted:
                continue
            blocks.append(generation.MODULE_TITLES[module].upper())
            blocks.append("")
            for req in drafted:
                blocks.append(req["draft"].strip())
                blocks.append("")
        update(opp_id, {"draft_text": "\n".join(blocks).strip()}, actor)
        set_generation_state(opp_id, "idle")
        audit.record(actor, "opportunity.generate", "opportunity", opp_id,
                     {"drafted": len(pending)})
    except Exception as exc:
        log.exception("generation failed for opportunity %s", opp_id)
        set_generation_state(opp_id, "error", str(exc))


async def load_amendment(opp_id: int, filename: str, data: bytes,
                         effective_date: str | None = None,
                         actor: str | None = None) -> dict:
    """An amendment supersedes the prior version and its new requirements are
    flagged, so nothing is quietly missed late in the cycle."""
    doc = documents.store(filename, data, opp_id=opp_id, doc_role="addendum",
                          doc_class="CLIENT_RFP", effective_date=effective_date, actor=actor)
    text = documents.get_text(doc["doc_id"])
    reqs = await generation.shred_requirements(text)
    added = add_requirements(opp_id, reqs, source_doc_id=doc["doc_id"],
                             from_amendment="Y", actor=actor)
    audit.record(actor, "opportunity.amendment", "opportunity", opp_id,
                 {"file": filename, "new_requirements": added})
    return {"document": doc, "new_requirements": added}
