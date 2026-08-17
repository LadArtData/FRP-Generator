"""Excel questionnaire round-trip.

Import a vendor workbook, detect its structure, answer every question row from
the governed answer library and the retrieval index with a confidence score, and
write the answers back into the original file so its formatting, data validation,
and dropdowns survive intact.

Column detection combines header keywords with structural evidence: a column
carrying a list-type data validation is almost certainly the response column,
and that beats a keyword guess. Detected ranges are parsed properly from the
worksheet's validation squares rather than pattern-matched from a string.
"""
from __future__ import annotations

import io
import json
import logging

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

from . import audit, classifier, documents, generation, opportunities
from .db import clob, cursor, transaction
from .errors import NotFound, ValidationFailed

log = logging.getLogger("harald.questionnaires")

import re

_QUESTION_HINT = re.compile(
    r"require|question|descriptio|feature|capabilit|functional|specif|criteri|item|need", re.I)
_RESPONSE_HINT = re.compile(
    r"response|rating|complian|\bmeets?\b|availab|vendor|\banswer\b|support|code|"
    r"\bmet\b|disposition|how (met|provided)", re.I)
_COMMENT_HINT = re.compile(
    r"comment|notes?|explanat|remark|approach|reference|detail|describe", re.I)

MAX_HEADER_SCAN_ROWS = 30
MAX_SCAN_COLUMNS = 60
MIN_QUESTION_LENGTH = 8
LOW_CONFIDENCE = 0.55


def _text(cell) -> str:
    if cell is None or cell.value is None:
        return ""
    return str(cell.value).strip()


def _list_validations(sheet: Worksheet) -> dict[int, list[str]]:
    """Map column index -> allowed values, from the sheet's own list validations.

    Ranges are parsed with openpyxl's range_boundaries, so a validation applied to
    C2:C500 correctly claims column C and nothing else. Formula-referenced lists
    (=Lists!$A$1:$A$6) are resolved by reading the referenced range.
    """
    columns: dict[int, list[str]] = {}
    workbook = sheet.parent

    for validation in sheet.data_validations.dataValidation:
        if validation.type != "list" or not validation.formula1:
            continue

        formula = validation.formula1.strip()
        values: list[str] = []
        if formula.startswith('"') and formula.endswith('"'):
            values = [v.strip() for v in formula[1:-1].split(",") if v.strip()]
        elif "!" in formula:
            try:
                sheet_ref, cell_ref = formula.lstrip("=").split("!", 1)
                source = workbook[sheet_ref.strip("'")]
                min_col, min_row, max_col, max_row = range_boundaries(
                    cell_ref.replace("$", "")
                )
                for row in source.iter_rows(min_row=min_row, max_row=max_row,
                                            min_col=min_col, max_col=max_col):
                    for cell in row:
                        if cell.value is not None and str(cell.value).strip():
                            values.append(str(cell.value).strip())
            except (KeyError, ValueError, TypeError) as exc:
                log.debug("could not resolve validation list %s: %s", formula, exc)
        if not values:
            continue

        for square in validation.sqref.ranges:
            bounds = range_boundaries(str(square))
            for column_index in range(bounds[0], bounds[2] + 1):
                columns.setdefault(column_index, values)

    return columns


def _detect(sheet: Worksheet) -> dict | None:
    """Find the header row and the question, response, and comment columns."""
    validated = _list_validations(sheet)
    scan_rows = min(sheet.max_row or 0, MAX_HEADER_SCAN_ROWS)
    scan_cols = min(sheet.max_column or 0, MAX_SCAN_COLUMNS)
    if not scan_rows or not scan_cols:
        return None

    best: tuple[int, dict[int, str]] | None = None
    best_score = 0
    for row_index in range(1, scan_rows + 1):
        headers: dict[int, str] = {}
        score = 0
        for column_index in range(1, scan_cols + 1):
            value = _text(sheet.cell(row=row_index, column=column_index))
            if not value or len(value) > 80:
                continue
            headers[column_index] = value
            if _QUESTION_HINT.search(value):
                score += 2
            if _RESPONSE_HINT.search(value) or _COMMENT_HINT.search(value):
                score += 1
        has_question = any(_QUESTION_HINT.search(v) for v in headers.values())
        if has_question and score > best_score:
            best, best_score = (row_index, headers), score

    if not best:
        return None

    header_row, headers = best

    question_col = next(
        (c for c, v in sorted(headers.items()) if _QUESTION_HINT.search(v)), None)
    if question_col is None:
        return None

    # Structural evidence wins: a validated column is the response column.
    response_col = next(
        (c for c in sorted(validated) if c != question_col and c in headers), None)
    if response_col is None:
        response_col = next(
            (c for c, v in sorted(headers.items())
             if c != question_col and _RESPONSE_HINT.search(v)), None)

    comment_col = next(
        (c for c, v in sorted(headers.items())
         if c not in (question_col, response_col) and _COMMENT_HINT.search(v)), None)

    return {
        "header_row": header_row,
        "question_col": question_col,
        "response_col": response_col,
        "comment_col": comment_col,
        "allowed_codes": validated.get(response_col, []) if response_col else [],
        "headers": {get_column_letter(c): v for c, v in headers.items()},
    }


def import_workbook(opp_id: int, source_doc_id: int, actor: str | None = None) -> dict:
    blob, filename = documents.get_blob(source_doc_id)
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise ValidationFailed(
            f"{filename} is not an Excel workbook. Import supports .xlsx and .xlsm.")

    workbook = load_workbook(io.BytesIO(blob), data_only=True)
    sheet_map: list[dict] = []
    items: list[dict] = []

    for sheet in workbook.worksheets:
        detected = _detect(sheet)
        if not detected:
            sheet_map.append({"sheet": sheet.title, "detected": False})
            continue

        question_letter = get_column_letter(detected["question_col"])
        response_letter = (get_column_letter(detected["response_col"])
                           if detected["response_col"] else None)
        comment_letter = (get_column_letter(detected["comment_col"])
                          if detected["comment_col"] else None)

        sheet_map.append({
            "sheet": sheet.title, "detected": True,
            "header_row": detected["header_row"], "question_col": question_letter,
            "response_col": response_letter, "comment_col": comment_letter,
            "allowed_codes": detected["allowed_codes"], "headers": detected["headers"],
        })

        for row_index in range(detected["header_row"] + 1, (sheet.max_row or 0) + 1):
            question = _text(sheet.cell(row=row_index, column=detected["question_col"]))
            if len(question) < MIN_QUESTION_LENGTH:
                continue
            items.append({
                "sheet": sheet.title, "row": row_index, "qcol": question_letter,
                "rcol": response_letter, "ccol": comment_letter,
                "question": question, "allowed": detected["allowed_codes"],
            })

    if not items:
        raise ValidationFailed(
            f"No question rows were detected in {filename}. The workbook may use an "
            f"unusual layout; the question column could not be identified.",
            {"sheet_map": sheet_map},
        )

    with transaction() as conn:
        cur = conn.cursor()
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_questionnaires
                 (opp_id, source_doc_id, filename, sheet_map, status, item_count)
               VALUES (:opp, :doc, :fn, :map, 'imported', :count)
               RETURNING q_id INTO :out""",
            {"opp": opp_id, "doc": source_doc_id, "fn": filename,
             "map": json.dumps(sheet_map), "count": len(items), "out": out},
        )
        q_id = out.getvalue()[0]
        # :row is Oracle reserved (ORA-01745); keep item binds on b_* names.
        cur.executemany(
            """INSERT INTO harald_questionnaire_items
                 (q_id, opp_id, sheet_name, row_index, question_col, response_col,
                  comment_col, question_text, allowed_codes, sort_order)
               VALUES (:b_q, :b_opp, :b_sheet, :b_row, :b_qcol, :b_rcol, :b_ccol,
                       :b_text, :b_codes, :b_ord)""",
            [
                {
                    "b_q": q_id,
                    "b_opp": opp_id,
                    "b_sheet": item["sheet"],
                    "b_row": item["row"],
                    "b_qcol": item["qcol"],
                    "b_rcol": item["rcol"],
                    "b_ccol": item["ccol"],
                    "b_text": item["question"],
                    "b_codes": json.dumps(item["allowed"]),
                    "b_ord": i,
                }
                for i, item in enumerate(items)
            ],
        )

    audit.record(actor, "questionnaire.import", "questionnaire", q_id,
                 {"file": filename, "items": len(items)})
    log.info("imported questionnaire q_id=%s file=%s items=%s", q_id, filename, len(items))
    return {"q_id": q_id, "filename": filename, "item_count": len(items),
            "sheet_map": sheet_map}


def list_for_opportunity(opp_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT q.q_id, q.filename, q.status, q.item_count, q.fill_error, q.created_at,
                      (SELECT COUNT(*) FROM harald_questionnaire_items i
                        WHERE i.q_id = q.q_id AND i.status <> 'todo'),
                      (SELECT COUNT(*) FROM harald_questionnaire_items i
                        WHERE i.q_id = q.q_id AND i.status = 'needs_review')
               FROM harald_questionnaires q WHERE q.opp_id = :o
               ORDER BY q.created_at DESC""",
            {"o": opp_id},
        )
        return [
            {"q_id": r[0], "filename": r[1], "status": r[2], "item_count": r[3],
             "fill_error": r[4], "created_at": r[5].isoformat() if r[5] else None,
             "answered": r[6], "needs_review": r[7]}
            for r in cur.fetchall()
        ]


def get(q_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT q_id, opp_id, source_doc_id, filename, sheet_map, status,
                      item_count, fill_error
               FROM harald_questionnaires WHERE q_id = :q""",
            {"q": q_id},
        )
        row = cur.fetchone()
        if not row:
            raise NotFound(f"Questionnaire {q_id} not found.")
        try:
            sheet_map = json.loads(clob(row[4])) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            sheet_map = []
        result = {"q_id": row[0], "opp_id": row[1], "source_doc_id": row[2],
                  "filename": row[3], "sheet_map": sheet_map, "status": row[5],
                  "item_count": row[6], "fill_error": row[7], "items": []}

        cur.execute(
            """SELECT qi_id, sheet_name, row_index, question_text, allowed_codes,
                      response_code, response_text, confidence, status, owner,
                      source_answer_id
               FROM harald_questionnaire_items WHERE q_id = :q ORDER BY sort_order""",
            {"q": q_id},
        )
        for r in cur.fetchall():
            try:
                allowed = json.loads(clob(r[4])) if r[4] else []
            except (json.JSONDecodeError, TypeError):
                allowed = []
            result["items"].append({
                "qi_id": r[0], "sheet": r[1], "row": r[2], "question": clob(r[3]),
                "allowed_codes": allowed, "response_code": r[5],
                "response_text": clob(r[6]), "confidence": r[7], "status": r[8],
                "owner": r[9], "source_answer_id": r[10],
            })
    return result


def set_status(q_id: int, status: str, error: str | None = None) -> None:
    with transaction() as conn:
        conn.cursor().execute(
            "UPDATE harald_questionnaires SET status = :s, fill_error = :e WHERE q_id = :q",
            {"s": status, "e": (error or "")[:2000] or None, "q": q_id},
        )


def update_item(qi_id: int, payload: dict, actor: str | None = None) -> None:
    allowed = {"response_code", "response_text", "status", "owner"}
    columns = {k: v for k, v in payload.items() if k in allowed}
    if "status" in columns and columns["status"] not in (
        "todo", "drafted", "needs_review", "approved"
    ):
        raise ValidationFailed("Invalid questionnaire item status.")
    if not columns:
        return
    with transaction() as conn:
        cur = conn.cursor()
        assignments = ", ".join(f"{col} = :{col}" for col in columns)
        cur.execute(
            f"UPDATE harald_questionnaire_items SET {assignments} WHERE qi_id = :qi",
            {**columns, "qi": qi_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Questionnaire item {qi_id} not found.")


def _persist_fill(qi_id: int, result: dict) -> None:
    status = "needs_review" if result["confidence"] < LOW_CONFIDENCE else "drafted"
    with transaction() as conn:
        conn.cursor().execute(
            """UPDATE harald_questionnaire_items
               SET response_code = :code, response_text = :text, confidence = :conf,
                   source_answer_id = :src, status = :status
               WHERE qi_id = :qi AND status <> 'approved'""",
            {"code": result["response_code"], "text": result["response_text"],
             "conf": result["confidence"], "src": result.get("source_answer_id"),
             "status": status, "qi": qi_id},
        )


async def fill(q_id: int, actor: str | None = None) -> None:
    """Answer every unapproved row. Concurrency is bounded inside the model client,
    so this runs as fast as the allowance permits without stampeding the API."""
    import asyncio

    try:
        set_status(q_id, "filling")
        questionnaire = get(q_id)
        pending = [i for i in questionnaire["items"] if i["status"] != "approved"]
        rfp_text, parsed_fields = "", {}
        if questionnaire.get("opp_id"):
            try:
                rfp_text, parsed_fields = opportunities.grounding_context(
                    opportunities.get(questionnaire["opp_id"]),
                )
            except Exception:
                log.debug("fill: no opp grounding for q_id=%s", q_id)

        async def answer_one(item: dict) -> None:
            module = classifier.module_of(item["question"])
            try:
                result = await generation.answer_question(
                    item["question"], module, item["allowed_codes"] or None,
                    rfp_text=rfp_text, parsed_fields=parsed_fields,
                )
            except Exception as exc:
                log.warning("fill failed qi_id=%s: %s", item["qi_id"], exc)
                # Never leave a blank "can't do it" cell — flag for a human.
                codes = item.get("allowed_codes") or []
                result = {
                    "response_code": generation._preferred_constructive_code(codes)
                    if codes else "",
                    "response_text": (
                        "[NEEDS HUMAN: fill failed automatically — "
                        f"{type(exc).__name__}. Complete this row manually.]"
                    ),
                    "confidence": 0.0,
                    "source_answer_id": None,
                }
            _persist_fill(item["qi_id"], result)

        await asyncio.gather(*(answer_one(item) for item in pending))
        set_status(q_id, "filled")
        audit.record(actor, "questionnaire.fill", "questionnaire", q_id,
                     {"items": len(pending)})
    except Exception as exc:
        log.exception("questionnaire fill failed q_id=%s", q_id)
        set_status(q_id, "error", str(exc))


def export(q_id: int) -> tuple[bytes, str]:
    """Write answers back into the original workbook. Loading without data_only
    preserves formulas, styles, and data validations, so the exported file is the
    agency's own workbook with iteria's answers in it."""
    questionnaire = get(q_id)
    blob, filename = documents.get_blob(questionnaire["source_doc_id"])
    workbook = load_workbook(io.BytesIO(blob))

    written = 0
    for item in questionnaire["items"]:
        if item["sheet"] not in workbook.sheetnames:
            continue
        sheet = workbook[item["sheet"]]
        with cursor() as cur:
            cur.execute(
                "SELECT response_col, comment_col FROM harald_questionnaire_items "
                "WHERE qi_id = :qi",
                {"qi": item["qi_id"]},
            )
            response_col, comment_col = cur.fetchone()
        if response_col and item["response_code"]:
            sheet[f"{response_col}{item['row']}"] = item["response_code"]
            written += 1
        if comment_col and item["response_text"]:
            sheet[f"{comment_col}{item['row']}"] = item["response_text"]

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    set_status(q_id, "exported")
    stem = filename.rsplit(".", 1)[0]
    log.info("exported questionnaire q_id=%s cells_written=%s", q_id, written)
    return buffer.read(), f"{stem}_iteria_response.xlsx"
