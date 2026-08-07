"""Freshness.

The answer library goes stale silently unless something forces it not to. Two
mechanisms keep it current:

  Review cadence. Every approved answer carries a review_due date. Answers past
  that date surface as overdue and can be swept into a review queue.

  Release impact. Oracle Fusion release notes are ingested and embedded. For each
  note, the semantically nearest approved answers are assessed by the model for
  whether the release actually changes what the answer claims. Genuinely affected
  answers are flagged for the owning SME. A capability that was a modification and
  is now standard is exactly the kind of drift that loses bids.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

from . import answers, audit, chunking, embeddings, llm, prompts
from .config import cfg
from .db import clob, cursor, transaction
from .errors import ValidationFailed

log = logging.getLogger("harald.freshness")

DEFAULT_REVIEW_MONTHS = 6
IMPACT_CANDIDATES = 5


def ingest_release_note(title: str, body: str, source: str | None = None,
                        release_version: str | None = None,
                        published: str | None = None, actor: str | None = None) -> int:
    if not body or len(body.strip()) < 40:
        raise ValidationFailed("The release note has no usable content.")

    published_date = None
    if published:
        try:
            published_date = date.fromisoformat(published.strip())
        except ValueError as exc:
            raise ValidationFailed("published must be YYYY-MM-DD.") from exc

    vector = embeddings.embed_passages([f"{title}\n{body[:4000]}"])[0]
    with transaction() as conn:
        cur = conn.cursor()
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_release_notes
                 (source, title, release_version, published_date, body, embedding)
               VALUES (:src, :title, :ver, :pub, :body, :vec)
               RETURNING note_id INTO :out""",
            {"src": source, "title": title[:400], "ver": release_version,
             "pub": published_date, "body": body, "vec": vector, "out": out},
        )
        note_id = out.getvalue()[0]
    audit.record(actor, "release_note.ingest", "release_note", note_id, {"title": title})
    return note_id


def ingest_release_document(filename: str, data: bytes, source: str | None = None,
                            release_version: str | None = None,
                            actor: str | None = None) -> dict:
    blocks = chunking.extract(filename, data)
    text = chunking.plain_text(blocks)
    note_id = ingest_release_note(
        title=filename, body=text, source=source or filename,
        release_version=release_version, actor=actor,
    )
    return {"note_id": note_id, "filename": filename, "characters": len(text)}


def list_notes(limit: int = 50) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT * FROM (
                 SELECT note_id, source, title, release_version, published_date, ingested_at,
                        DBMS_LOB.SUBSTR(body, 300, 1)
                 FROM harald_release_notes ORDER BY ingested_at DESC
               ) WHERE ROWNUM <= :lim""",
            {"lim": limit},
        )
        return [
            {"note_id": r[0], "source": r[1], "title": r[2], "release_version": r[3],
             "published_date": r[4].strftime("%Y-%m-%d") if r[4] else None,
             "ingested_at": r[5].isoformat() if r[5] else None, "preview": r[6]}
            for r in cur.fetchall()
        ]


def _nearest_answers(note_id: int, limit: int = IMPACT_CANDIDATES) -> list[dict]:
    """Approved answers closest to a release note in embedding space."""
    with cursor() as cur:
        cur.execute(
            """SELECT * FROM (
                 SELECT a.ans_id, a.question_canonical, a.answer_text, a.module_tag,
                        a.owner_sme,
                        VECTOR_DISTANCE(a.embedding, n.embedding, COSINE) AS distance
                 FROM harald_answers a
                 CROSS JOIN (SELECT embedding FROM harald_release_notes WHERE note_id = :n) n
                 WHERE a.status = 'approved' AND a.embedding IS NOT NULL
                 ORDER BY distance
               ) WHERE ROWNUM <= :lim""",
            {"n": note_id, "lim": limit},
        )
        return [
            {"ans_id": r[0], "question": r[1], "answer": clob(r[2]), "module": r[3],
             "owner_sme": r[4], "distance": round(float(r[5]), 4)}
            for r in cur.fetchall()
        ]


def _note_body(note_id: int) -> tuple[str, str]:
    with cursor() as cur:
        cur.execute("SELECT title, body FROM harald_release_notes WHERE note_id = :n",
                    {"n": note_id})
        row = cur.fetchone()
    if not row:
        raise ValidationFailed(f"Release note {note_id} not found.")
    return row[0], clob(row[1])


async def assess_impact(note_id: int, actor: str | None = None) -> dict:
    """Assess which approved answers the release genuinely invalidates, and flag
    those for SME review by pulling their review date forward."""
    title, body = _note_body(note_id)
    candidates = _nearest_answers(note_id)
    if not candidates:
        return {"note_id": note_id, "assessed": 0, "affected": []}

    async def assess(candidate: dict) -> dict:
        user = (
            f"RELEASE NOTE: {title}\n{body[:5000]}\n\n"
            f"EXISTING STANDING ANSWER\n"
            f"Question: {candidate['question']}\n"
            f"Answer: {candidate['answer'][:2500]}\n\n"
            "Does this release change what the answer claims? Return the JSON object now."
        )
        try:
            verdict = await llm.complete_json(
                prompts.RELEASE_IMPACT_SYSTEM, user, expect=dict,
                model=cfg.draft_model, max_tokens=500,
            )
        except Exception as exc:
            log.warning("impact assessment failed ans_id=%s: %s", candidate["ans_id"], exc)
            return {**candidate, "affected": False, "reason": f"assessment failed: {exc}"}
        return {
            **candidate,
            "affected": bool(verdict.get("affected")),
            "reason": str(verdict.get("reason", ""))[:400],
            "suggested_update": str(verdict.get("suggested_update", ""))[:600],
        }

    results = await asyncio.gather(*(assess(c) for c in candidates))
    affected = [r for r in results if r["affected"]]

    if affected:
        with transaction() as conn:
            cur = conn.cursor()
            cur.executemany(
                """UPDATE harald_answers
                   SET review_due = TRUNC(SYSDATE),
                       source_refs = SUBSTR(
                         NVL(source_refs || ' | ', '') || 'release:' || :note, 1, 1000),
                       updated_at = SYSTIMESTAMP
                   WHERE ans_id = :ans""",
                [{"note": str(note_id), "ans": r["ans_id"]} for r in affected],
            )
        audit.record(actor, "release_note.impact", "release_note", note_id,
                     {"affected": [r["ans_id"] for r in affected]})

    log.info("release note %s: %s of %s answers affected",
             note_id, len(affected), len(results))
    return {
        "note_id": note_id, "title": title, "assessed": len(results),
        "affected": [
            {"ans_id": r["ans_id"], "question": r["question"], "module": r["module"],
             "owner_sme": r["owner_sme"], "reason": r["reason"],
             "suggested_update": r["suggested_update"]}
            for r in affected
        ],
    }


def review_queue() -> dict:
    """Answers that need an SME look: past their review date, or never dated."""
    with cursor() as cur:
        cur.execute(
            """SELECT ans_id, question_canonical, module_tag, owner_sme, review_due,
                      effective_date, times_used, source_refs
               FROM harald_answers
               WHERE status = 'approved'
                 AND (review_due IS NULL OR review_due <= TRUNC(SYSDATE))
               ORDER BY NVL(review_due, DATE '1900-01-01'), times_used DESC"""
        )
        overdue = [
            {"ans_id": r[0], "question_canonical": r[1], "module_tag": r[2],
             "owner_sme": r[3],
             "review_due": r[4].strftime("%Y-%m-%d") if r[4] else None,
             "effective_date": r[5].strftime("%Y-%m-%d") if r[5] else None,
             "times_used": r[6], "source_refs": r[7]}
            for r in cur.fetchall()
        ]
        cur.execute(
            """SELECT COUNT(*) FROM harald_answers
               WHERE status = 'approved' AND review_due > TRUNC(SYSDATE)"""
        )
        current_count = cur.fetchone()[0]
    return {"overdue": overdue, "overdue_count": len(overdue), "current_count": current_count}


def mark_reviewed(ans_id: int, months: int = DEFAULT_REVIEW_MONTHS,
                  actor: str | None = None) -> dict:
    """Confirm an answer is still accurate and push its next review out."""
    next_due = date.today() + timedelta(days=30 * months)
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE harald_answers SET review_due = :due, updated_at = SYSTIMESTAMP "
            "WHERE ans_id = :a",
            {"due": next_due, "a": ans_id},
        )
        if cur.rowcount == 0:
            raise ValidationFailed(f"Answer {ans_id} not found.")
    audit.record(actor, "answer.reviewed", "answer", ans_id,
                 {"next_review": next_due.isoformat()})
    return answers.get(ans_id)
