"""Governed answer library.

Curated, approved, SME-owned standing answers, kept separate from raw proposal
prose. Only approved answers auto-answer anything. Deprecated answers are
excluded from every path, which is what makes freshness enforceable.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import embeddings
from .config import cfg
from .db import clob, cursor, transaction
from .errors import NotFound, ValidationFailed

log = logging.getLogger("harald.answers")

STATUSES = ("draft", "approved", "deprecated")


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    raise ValidationFailed(f"Unrecognised date: {value!r}. Use YYYY-MM-DD.")


def _embedding_for(question: str, answer: str):
    return embeddings.embed_passages([f"{question}\n{answer}"])[0]


def create(payload: dict, actor: str | None = None) -> int:
    question = (payload.get("question_canonical") or "").strip()
    answer = (payload.get("answer_text") or "").strip()
    if not question:
        raise ValidationFailed("A canonical question is required.")
    if not answer:
        raise ValidationFailed("An answer is required.")
    status = payload.get("status", "draft")
    if status not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")

    with transaction() as conn:
        cur = conn.cursor()
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_answers
                 (question_canonical, answer_text, module_tag, tags, owner_sme, status,
                  effective_date, review_due, source_refs, embedding)
               VALUES (:q, :a, :mod, :tags, :owner, :status, :eff, :review, :src, :vec)
               RETURNING ans_id INTO :out""",
            {"q": question[:1000], "a": answer,
             "mod": (payload.get("module_tag") or "GENERAL").upper(),
             "tags": payload.get("tags"), "owner": payload.get("owner_sme") or actor,
             "status": status, "eff": _parse_date(payload.get("effective_date")),
             "review": _parse_date(payload.get("review_due")),
             "src": payload.get("source_refs"),
             "vec": _embedding_for(question, answer), "out": out},
        )
        return out.getvalue()[0]


def update(ans_id: int, payload: dict) -> None:
    columns = {
        "question_canonical": payload.get("question_canonical"),
        "answer_text": payload.get("answer_text"),
        "module_tag": (payload["module_tag"].upper() if payload.get("module_tag") else None),
        "tags": payload.get("tags"),
        "owner_sme": payload.get("owner_sme"),
        "status": payload.get("status"),
        "source_refs": payload.get("source_refs"),
    }
    columns = {k: v for k, v in columns.items() if v is not None}
    if "status" in columns and columns["status"] not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")
    if "effective_date" in payload:
        columns["effective_date"] = _parse_date(payload["effective_date"])
    if "review_due" in payload:
        columns["review_due"] = _parse_date(payload["review_due"])
    if not columns:
        return

    with transaction() as conn:
        cur = conn.cursor()
        assignments = ", ".join(f"{col} = :{col}" for col in columns)
        cur.execute(
            f"UPDATE harald_answers SET {assignments}, updated_at = SYSTIMESTAMP "
            f"WHERE ans_id = :ans_id",
            {**columns, "ans_id": ans_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Answer {ans_id} not found.")
        if "answer_text" in columns or "question_canonical" in columns:
            cur.execute(
                "SELECT question_canonical, answer_text FROM harald_answers WHERE ans_id = :a",
                {"a": ans_id},
            )
            question, answer = cur.fetchone()
            cur.execute(
                "UPDATE harald_answers SET embedding = :vec WHERE ans_id = :a",
                {"vec": _embedding_for(question, clob(answer)), "a": ans_id},
            )


def get(ans_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT ans_id, question_canonical, answer_text, module_tag, tags, owner_sme,
                      status, effective_date, review_due, source_refs, times_used
               FROM harald_answers WHERE ans_id = :a""",
            {"a": ans_id},
        )
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Answer {ans_id} not found.")
    return {"ans_id": row[0], "question_canonical": row[1], "answer_text": clob(row[2]),
            "module_tag": row[3], "tags": row[4], "owner_sme": row[5], "status": row[6],
            "effective_date": row[7].strftime("%Y-%m-%d") if row[7] else None,
            "review_due": row[8].strftime("%Y-%m-%d") if row[8] else None,
            "source_refs": row[9], "times_used": row[10]}


def list_answers(status: str | None = None, module: str | None = None,
                 query: str | None = None, limit: int = 200) -> list[dict]:
    where = ["1 = 1"]
    binds: dict = {"lim": limit}
    if status:
        where.append("status = :status")
        binds["status"] = status
    if module:
        where.append("module_tag = :module")
        binds["module"] = module.upper()
    if query:
        where.append(
            "(LOWER(question_canonical) LIKE :q "
            "OR LOWER(DBMS_LOB.SUBSTR(answer_text, 3000, 1)) LIKE :q "
            "OR LOWER(NVL(tags, ' ')) LIKE :q)"
        )
        binds["q"] = f"%{query.lower()}%"

    sql = f"""SELECT * FROM (
                SELECT ans_id, question_canonical, module_tag, tags, owner_sme, status,
                       effective_date, review_due, times_used,
                       DBMS_LOB.SUBSTR(answer_text, 400, 1)
                FROM harald_answers WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC
              ) WHERE ROWNUM <= :lim"""
    with cursor() as cur:
        cur.execute(sql, binds)
        return [
            {"ans_id": r[0], "question_canonical": r[1], "module_tag": r[2], "tags": r[3],
             "owner_sme": r[4], "status": r[5],
             "effective_date": r[6].strftime("%Y-%m-%d") if r[6] else None,
             "review_due": r[7].strftime("%Y-%m-%d") if r[7] else None,
             "times_used": r[8], "preview": r[9]}
            for r in cur.fetchall()
        ]


def stats() -> dict:
    with cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM harald_answers GROUP BY status")
        counts = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(*) FROM harald_answers "
            "WHERE status = 'approved' AND review_due IS NOT NULL AND review_due <= SYSDATE"
        )
        overdue = cur.fetchone()[0]
    return {"total": sum(counts.values()), "approved": counts.get("approved", 0),
            "draft": counts.get("draft", 0), "deprecated": counts.get("deprecated", 0),
            "review_overdue": overdue}


def best_match(question: str, module: str | None = None) -> dict | None:
    """Nearest approved answer. Deprecated and draft answers are never returned."""
    if not question or not question.strip():
        return None
    vector = embeddings.embed_query(question)
    sql = """
        SELECT * FROM (
          SELECT ans_id, question_canonical, answer_text, module_tag,
                 VECTOR_DISTANCE(embedding, :qvec, COSINE) *
                   CASE WHEN :module IS NOT NULL AND module_tag = :module THEN 0.85
                        ELSE 1.0 END AS distance
          FROM harald_answers
          WHERE status = 'approved' AND embedding IS NOT NULL
          ORDER BY distance
        ) WHERE ROWNUM <= 1
    """
    with cursor() as cur:
        cur.execute(sql, {"qvec": vector, "module": module.upper() if module else None})
        row = cur.fetchone()
    if not row:
        return None
    return {"ans_id": row[0], "question_canonical": row[1], "answer_text": clob(row[2]),
            "module_tag": row[3], "score": round(float(row[4]), 4),
            "strong": float(row[4]) <= cfg.strong_match_distance}


def mark_used(ans_id: int) -> None:
    with transaction() as conn:
        conn.cursor().execute(
            "UPDATE harald_answers SET times_used = times_used + 1, "
            "last_used_at = SYSTIMESTAMP WHERE ans_id = :a",
            {"a": ans_id},
        )
