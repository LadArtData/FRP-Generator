"""Fillable pricing matrix for Brian.

Numbers stay editable. AI suggests from similar past matrices (industry,
engagement type, modules) so the more Brian saves, the better the next fill.
Approve / lock stays approver-only.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict

import oracledb

from . import audit, engagement, opportunities
from .config import cfg
from .db import clob, cursor, transaction
from .errors import Conflict, Forbidden, NotFound, ValidationFailed

log = logging.getLogger("harald.pricing_matrix")

STATUSES = ("draft", "suggested", "reviewed", "approved")

DEFAULT_LINES = [
    {"category": "Delivery", "line_item": "Project management", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Delivery", "line_item": "Solution architecture", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Functional", "line_item": "Financials configuration", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Functional", "line_item": "HCM configuration", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Functional", "line_item": "Payroll configuration", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Functional", "line_item": "Procurement / P2P configuration", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Functional", "line_item": "Budget / EPM configuration", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Technical", "line_item": "Integrations & interfaces", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Technical", "line_item": "Security / SSO / identity", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Data", "line_item": "Data conversion & reconciliation", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Quality", "line_item": "Testing / SIT / UAT support", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Enablement", "line_item": "Training & knowledge transfer", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Cutover", "line_item": "Cutover & hypercare", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Travel & expenses", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Contingency", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
]


AI_ENABLEMENT_LINES = [
    {"category": "Discovery", "line_item": "AI readiness assessment workshops", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Discovery", "line_item": "Current-state data & governance review", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Strategy", "line_item": "Enterprise AI roadmap development", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Governance", "line_item": "AI governance operating model (HIPAA-aware)", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Enablement", "line_item": "Staff training & responsible AI workshops", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Enablement", "line_item": "Executive & stakeholder facilitation", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Delivery", "line_item": "Project management", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Delivery", "line_item": "Solution architecture / technical advisory", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Transition", "line_item": "Documentation & transition support", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Travel & expenses", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Contingency", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
]


# Public-sector consulting that is neither an Oracle module rollout nor an AI
# programme — needs assessments, procurement support, process work. Naming ERP
# modules on one of these bids is a tell that nobody read the solicitation.
CONSULTING_LINES = [
    {"category": "Discovery", "line_item": "Current-state assessment & stakeholder interviews",
     "unit": "hours", "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Discovery", "line_item": "Requirements definition & documentation", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Analysis", "line_item": "Business process analysis & redesign", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Analysis", "line_item": "Alternatives analysis & recommendations", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Advisory", "line_item": "Procurement & solicitation support", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Advisory", "line_item": "Implementation oversight & quality assurance",
     "unit": "hours", "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Delivery", "line_item": "Project management", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Enablement", "line_item": "Stakeholder facilitation & workshops", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Enablement", "line_item": "Training & knowledge transfer", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Transition", "line_item": "Documentation & transition support", "unit": "hours",
     "qty": None, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Travel & expenses", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
    {"category": "Other", "line_item": "Contingency", "unit": "lump",
     "qty": 1, "rate": None, "amount": None, "notes": "", "ai_suggested": False},
]


_ORA_NAME_ALREADY_USED = 955

# DDL is a startup concern, not a per-request one. Once the objects are known to
# be in place this process stops issuing CREATE statements entirely.
_table_ready = False


def _ddl_statements(schema: str) -> list[tuple[str, str]]:
    """(object name, statement) pairs, each schema-qualified.

    Qualifying every name is the point. The previous version issued unqualified
    DDL, so the create target was CURRENT_SCHEMA while the existence check
    looked at ``cfg.app_schema``. Any drift between those two — a table created
    by hand as ADMIN, a session whose callback had not run — made the check
    permanently false and the create permanently collide, which is an ORA-00955
    on every single request forever.
    """
    table = f"{schema}.harald_pricing_matrix"
    return [
        (table, f"""CREATE TABLE {table} (
                     matrix_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                     opp_id          NUMBER NOT NULL,
                     price_id        NUMBER,
                     engagement_type VARCHAR2(80),
                     industry        VARCHAR2(120),
                     modules         VARCHAR2(400),
                     client_name     VARCHAR2(200),
                     lines_json      CLOB NOT NULL,
                     total_amount    NUMBER,
                     currency        VARCHAR2(8) DEFAULT 'USD' NOT NULL,
                     status          VARCHAR2(20) DEFAULT 'draft' NOT NULL,
                     suggested_from  CLOB,
                     locked          CHAR(1) DEFAULT 'N' NOT NULL,
                     owner           VARCHAR2(80),
                     created_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
                     updated_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
                     CONSTRAINT harald_pmat_status_ck CHECK (status IN
                       ('draft','suggested','reviewed','approved')),
                     CONSTRAINT harald_pmat_locked_ck CHECK (locked IN ('Y','N'))
                   )"""),
        (f"{schema}.harald_pmat_opp_idx",
         f"CREATE INDEX {schema}.harald_pmat_opp_idx "
         f"ON {table}(opp_id, updated_at DESC)"),
        # suggest() filters on status and orders by updated_at; without this the
        # peer scan is a full table scan.
        (f"{schema}.harald_pmat_status_idx",
         f"CREATE INDEX {schema}.harald_pmat_status_idx "
         f"ON {table}(status, industry)"),
    ]


def ensure_table() -> None:
    """Create the pricing matrix table and its indexes if they are missing.

    Idempotent by construction rather than by prediction: each statement runs in
    its own transaction and an ORA-00955 on that statement means "already
    there", which is success. Nothing is dropped or replaced — this table holds
    approved, locked pricing and there is no backup path for it.
    """
    global _table_ready
    if _table_ready:
        return

    schema = cfg.app_schema  # validated as an identifier in config.validate()
    for name, statement in _ddl_statements(schema):
        try:
            with transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(statement)
            log.info("created %s", name)
        except oracledb.DatabaseError as exc:
            err = exc.args[0] if exc.args else None
            if getattr(err, "code", None) != _ORA_NAME_ALREADY_USED:
                # ORA-01031 insufficient privileges, ORA-00942, connection
                # failures — all must surface. Swallowing them is what let a
                # missing table look like a working one.
                raise
            log.debug("%s already present", name)
    _table_ready = True


def default_lines_for(opp_id: int) -> list[dict]:
    """Starting line items matched to what the bid actually is.

    There used to be two shapes here — AI enablement, and everything else gets
    the Oracle ERP module list. That put "Financials configuration", "HCM
    configuration" and "Payroll configuration" on the Jefferson County Sheriff's
    Office and Town of Salem bids, which are public-sector consulting
    engagements with no ERP modules in scope. A pricing sheet that names work
    the client never asked for is worse than an empty one: it reads as a
    template nobody looked at.
    """
    opp = opportunities.get(opp_id)
    rfp_text, parsed = opportunities.grounding_context(opp)
    profile = engagement.classify_opportunity(parsed, rfp_text)
    if profile.kind == "ai_enablement":
        return [dict(row) for row in AI_ENABLEMENT_LINES]
    if profile.kind == "erp_modernization":
        return [dict(row) for row in DEFAULT_LINES]
    if profile.kind == "mixed":
        # Oracle delivery plus the AI advisory work, de-duplicated by line item.
        merged, seen = [], set()
        for row in list(DEFAULT_LINES) + list(AI_ENABLEMENT_LINES):
            key = row["line_item"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(row))
        return merged
    return [dict(row) for row in CONSULTING_LINES]


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _line_amount(line: dict) -> float | None:
    amount = _num(line.get("amount"))
    if amount is not None:
        return amount
    qty, rate = _num(line.get("qty")), _num(line.get("rate"))
    if qty is not None and rate is not None:
        return round(qty * rate, 2)
    return None


def _normalize_lines(lines: list | None) -> list[dict]:
    if not lines:
        return [dict(row) for row in DEFAULT_LINES]
    out = []
    for raw in lines:
        if not isinstance(raw, dict):
            continue
        item = {
            "category": str(raw.get("category") or "Other")[:80],
            "line_item": str(raw.get("line_item") or "").strip()[:200],
            "unit": str(raw.get("unit") or "hours")[:40],
            "qty": _num(raw.get("qty")),
            "rate": _num(raw.get("rate")),
            "amount": _num(raw.get("amount")),
            "notes": str(raw.get("notes") or "")[:500],
            "ai_suggested": bool(raw.get("ai_suggested")),
        }
        if not item["line_item"]:
            continue
        computed = _line_amount(item)
        if computed is not None:
            item["amount"] = computed
        out.append(item)
    return out or [dict(row) for row in DEFAULT_LINES]


def _total(lines: list[dict]) -> float:
    return round(sum(_line_amount(line) or 0.0 for line in lines), 2)


def _context_from_opp(opp_id: int) -> dict:
    opp = opportunities.get(opp_id)
    form = {}
    try:
        extracted = json.loads(opp.get("extracted_json") or "null") or {}
        form = {**(extracted.get("parsed_fields") or {}),
                **(extracted.get("studio_form") or {})}
    except (json.JSONDecodeError, TypeError):
        form = {}
    modules = form.get("proposed_modules") or form.get("required_modules") or []
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.replace(";", ",").split(",") if m.strip()]
    rfp_text, parsed = opportunities.grounding_context(opp)
    profile = engagement.classify_opportunity({**form, **parsed}, rfp_text)
    engagement_label = form.get("engagement_type") or profile.label
    return {
        "client_name": opp.get("client_name") or form.get("client_name"),
        "industry": form.get("industry") or opp.get("agency"),
        "engagement_type": engagement_label,
        "modules": ", ".join(str(m) for m in modules)[:400],
    }


def _row_to_dict(row) -> dict:
    try:
        lines = json.loads(clob(row[7]) or "[]")
    except (json.JSONDecodeError, TypeError):
        lines = []
    try:
        suggested = json.loads(clob(row[11]) or "null") if row[11] else None
    except (json.JSONDecodeError, TypeError):
        suggested = None
    return {
        "matrix_id": row[0], "opp_id": row[1], "price_id": row[2],
        "engagement_type": row[3], "industry": row[4], "modules": row[5],
        "client_name": row[6], "lines": _normalize_lines(lines),
        "total_amount": float(row[8]) if row[8] is not None else _total(lines),
        "currency": row[9] or "USD", "status": row[10],
        "suggested_from": suggested, "locked": row[12], "owner": row[13],
        "created_at": row[14].isoformat() if row[14] else None,
        "updated_at": row[15].isoformat() if row[15] else None,
    }


_SELECT = """SELECT matrix_id, opp_id, price_id, engagement_type, industry, modules,
                    client_name, lines_json, total_amount, currency, status,
                    suggested_from, locked, owner, created_at, updated_at
             FROM harald_pricing_matrix"""


def get_for_opportunity(opp_id: int) -> dict:
    ensure_table()
    saved = None
    with cursor() as cur:
        cur.execute(
            f"{_SELECT} WHERE opp_id = :o ORDER BY updated_at DESC "
            f"FETCH FIRST 1 ROWS ONLY",
            {"o": opp_id},
        )
        row = cur.fetchone()
        # lines_json and suggested_from are CLOBs. _row_to_dict reads both
        # locators, so it has to run before the pool takes the connection back —
        # otherwise the first time anyone reloads a bid whose matrix has been
        # saved, section 07 dies. It has not fired yet only because every bid
        # currently has matrix_id None and never reaches this branch.
        if row:
            saved = _row_to_dict(row)
    if saved is not None:
        return saved
    ctx = _context_from_opp(opp_id)
    lines = default_lines_for(opp_id)
    return {
        "matrix_id": None, "opp_id": opp_id, "price_id": None,
        "engagement_type": ctx["engagement_type"], "industry": ctx["industry"],
        "modules": ctx["modules"], "client_name": ctx["client_name"],
        "lines": lines, "total_amount": 0, "currency": "USD",
        "status": "draft", "suggested_from": None, "locked": "N",
        "owner": None, "created_at": None, "updated_at": None,
        "is_new": True,
    }


def save(opp_id: int, payload: dict, actor: str, *, as_approver: bool = False) -> dict:
    ensure_table()
    existing = get_for_opportunity(opp_id)
    if existing.get("locked") == "Y" and not as_approver:
        raise Conflict("Pricing matrix is locked. Brian must unlock it to edit.")
    if existing.get("status") == "approved" and not as_approver:
        raise Forbidden("Only the approver can change an approved pricing matrix.")

    ctx = _context_from_opp(opp_id)
    lines = _normalize_lines(payload.get("lines") or existing.get("lines"))
    # Clearing ai_suggested when human edits a cell is handled by the UI; keep flags.
    total = _total(lines)
    status = payload.get("status") or existing.get("status") or "draft"
    if status not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")
    if status == "approved" and not as_approver:
        raise Forbidden("Only the approver can approve pricing.")

    # Named engagement_type, not engagement: the bare name shadows the imported
    # engagement module for the rest of this function.
    engagement_type = payload.get("engagement_type") or ctx["engagement_type"]
    industry = payload.get("industry") or ctx["industry"]
    modules = payload.get("modules") or ctx["modules"]
    client = payload.get("client_name") or ctx["client_name"]
    currency = payload.get("currency") or "USD"
    locked = "Y" if status == "approved" else (payload.get("locked") or existing.get("locked") or "N")
    if locked not in ("Y", "N"):
        locked = "N"

    lines_json = json.dumps(lines)
    suggested = payload.get("suggested_from", existing.get("suggested_from"))
    suggested_json = json.dumps(suggested) if suggested is not None else None

    with transaction() as conn:
        cur = conn.cursor()
        if existing.get("matrix_id"):
            cur.execute(
                """UPDATE harald_pricing_matrix
                   SET engagement_type = :eng, industry = :ind, modules = :mod,
                       client_name = :client, lines_json = :lines, total_amount = :tot,
                       currency = :cur, status = :status, suggested_from = :sug,
                       locked = :locked, owner = :owner, updated_at = SYSTIMESTAMP
                   WHERE matrix_id = :id""",
                {"eng": engagement_type, "ind": industry, "mod": modules, "client": client,
                 "lines": lines_json, "tot": total, "cur": currency, "status": status,
                 "sug": suggested_json, "locked": locked, "owner": actor,
                 "id": existing["matrix_id"]},
            )
            matrix_id = existing["matrix_id"]
        else:
            out = cur.var(int)
            cur.execute(
                """INSERT INTO harald_pricing_matrix
                     (opp_id, engagement_type, industry, modules, client_name, lines_json,
                      total_amount, currency, status, suggested_from, locked, owner)
                   VALUES (:opp, :eng, :ind, :mod, :client, :lines, :tot, :cur, :status,
                           :sug, :locked, :owner)
                   RETURNING matrix_id INTO :out""",
                {"opp": opp_id, "eng": engagement_type, "ind": industry, "mod": modules,
                 "client": client, "lines": lines_json, "tot": total, "cur": currency,
                 "status": status, "sug": suggested_json, "locked": locked,
                 "owner": actor, "out": out},
            )
            matrix_id = out.getvalue()[0]

    audit.record(actor, "pricing_matrix.save", "pricing_matrix", matrix_id,
                 {"opp_id": opp_id, "total": total, "status": status})
    return get_for_opportunity(opp_id)


def _similarity(candidate: dict, target: dict) -> float:
    score = 0.0
    if candidate.get("industry") and target.get("industry"):
        if str(candidate["industry"]).lower() == str(target["industry"]).lower():
            score += 3.0
        elif str(target["industry"]).lower() in str(candidate["industry"]).lower():
            score += 1.5
    if candidate.get("engagement_type") and target.get("engagement_type"):
        if str(candidate["engagement_type"]).lower() == str(target["engagement_type"]).lower():
            score += 2.0
    cmods = {m.strip().lower() for m in str(candidate.get("modules") or "").split(",") if m.strip()}
    tmods = {m.strip().lower() for m in str(target.get("modules") or "").split(",") if m.strip()}
    if cmods and tmods:
        score += 2.0 * len(cmods & tmods) / max(len(tmods), 1)
    if candidate.get("status") == "approved":
        score += 1.0
    return score


def suggest(opp_id: int, actor: str) -> dict:
    """Fill rates/qty from similar past matrices. Cells stay editable."""
    ensure_table()
    current = get_for_opportunity(opp_id)
    target = {
        "industry": current.get("industry"),
        "engagement_type": current.get("engagement_type"),
        "modules": current.get("modules"),
    }
    with cursor() as cur:
        cur.execute(
            f"{_SELECT} WHERE opp_id <> :o AND status IN ('approved','reviewed','suggested','draft') "
            f"ORDER BY updated_at DESC FETCH FIRST 80 ROWS ONLY",
            {"o": opp_id},
        )
        history = [_row_to_dict(r) for r in cur.fetchall()]

    ranked = sorted(
        (( _similarity(h, target), h) for h in history),
        key=lambda pair: pair[0], reverse=True,
    )
    peers = [h for score, h in ranked if score >= 1.0][:8]
    if not peers:
        peers = [h for _, h in ranked[:5]]

    rates: dict[str, list[float]] = defaultdict(list)
    qtys: dict[str, list[float]] = defaultdict(list)
    for peer in peers:
        for line in peer.get("lines") or []:
            key = (line.get("line_item") or "").strip().lower()
            if not key:
                continue
            if _num(line.get("rate")) is not None:
                rates[key].append(_num(line["rate"]))
            if _num(line.get("qty")) is not None:
                qtys[key].append(_num(line["qty"]))

    filled = 0
    lines = []
    for line in current.get("lines") or DEFAULT_LINES:
        item = dict(line)
        key = (item.get("line_item") or "").strip().lower()
        changed = False
        if key in rates and (item.get("rate") is None or item.get("ai_suggested")):
            item["rate"] = round(statistics.median(rates[key]), 2)
            changed = True
        if key in qtys and (item.get("qty") is None or item.get("ai_suggested")):
            item["qty"] = round(statistics.median(qtys[key]), 1)
            changed = True
        if changed:
            item["ai_suggested"] = True
            item["amount"] = _line_amount(item)
            filled += 1
        lines.append(item)

    meta = {
        "peers_used": [
            {"opp_id": p["opp_id"], "client_name": p.get("client_name"),
             "industry": p.get("industry"), "total_amount": p.get("total_amount"),
             "status": p.get("status")}
            for p in peers[:5]
        ],
        "filled_cells": filled,
        "method": "median_of_similar_matrices",
    }
    saved = save(
        opp_id,
        {
            "lines": lines,
            "status": "suggested" if current.get("status") == "draft" else current.get("status"),
            "suggested_from": meta,
            "engagement_type": current.get("engagement_type"),
            "industry": current.get("industry"),
            "modules": current.get("modules"),
        },
        actor,
        as_approver=False,
    )
    audit.record(actor, "pricing_matrix.suggest", "pricing_matrix",
                 saved.get("matrix_id"), meta)
    return saved


def approve(opp_id: int, actor: str) -> dict:
    ensure_table()
    current = get_for_opportunity(opp_id)
    if not current.get("matrix_id"):
        raise ValidationFailed("Save the pricing matrix before approving.")
    return save(
        opp_id,
        {"lines": current["lines"], "status": "approved", "locked": "Y",
         "suggested_from": current.get("suggested_from")},
        actor,
        as_approver=True,
    )


def unlock(opp_id: int, actor: str) -> dict:
    ensure_table()
    current = get_for_opportunity(opp_id)
    if not current.get("matrix_id"):
        raise NotFound("No pricing matrix to unlock.")
    with transaction() as conn:
        conn.cursor().execute(
            """UPDATE harald_pricing_matrix
               SET locked = 'N', status = 'reviewed', updated_at = SYSTIMESTAMP,
                   owner = :owner
               WHERE matrix_id = :id""",
            {"owner": actor, "id": current["matrix_id"]},
        )
    audit.record(actor, "pricing_matrix.unlock", "pricing_matrix", current["matrix_id"])
    return get_for_opportunity(opp_id)


def to_csv(opp_id: int) -> str:
    """Plain CSV of the current matrix for materials zip / Brian review."""
    import csv
    import io

    matrix = get_for_opportunity(opp_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "category", "line_item", "unit", "qty", "rate", "amount", "notes",
        "ai_suggested", "status", "industry", "engagement_type", "total_amount",
    ])
    for line in matrix.get("lines") or []:
        writer.writerow([
            line.get("category"), line.get("line_item"), line.get("unit"),
            line.get("qty"), line.get("rate"), line.get("amount"),
            line.get("notes"), "Y" if line.get("ai_suggested") else "N",
            matrix.get("status"), matrix.get("industry"),
            matrix.get("engagement_type"), matrix.get("total_amount"),
        ])
    return buf.getvalue()
