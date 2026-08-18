"""Seed the governed answer bank from starter Q&A and won library chunks."""
from __future__ import annotations

import json
import logging
import os
import re

from . import answers
from .db import clob, cursor

log = logging.getLogger("harald.answer_seed")

_SEED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "answer_bank_seed.json"
)


def _exists(question: str) -> bool:
    with cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) FROM harald_answers
               WHERE LOWER(question_canonical) = LOWER(:q)""",
            {"q": question[:1000]},
        )
        return int(cur.fetchone()[0]) > 0


def seed_starter(status: str = "approved", actor: str = "studio") -> dict:
    """Load standard public-sector Fusion Q&A into harald_answers."""
    with open(_SEED_PATH, encoding="utf-8") as handle:
        rows = json.load(handle)
    created = 0
    skipped = 0
    for row in rows:
        question = (row.get("question_canonical") or "").strip()
        if not question or _exists(question):
            skipped += 1
            continue
        answers.create(
            {
                "question_canonical": question,
                "answer_text": row.get("answer_text"),
                "module_tag": row.get("module_tag") or "GENERAL",
                "tags": row.get("tags"),
                "status": status,
                "owner_sme": actor,
                "source_refs": "seed:answer_bank_seed.json",
            },
            actor,
        )
        created += 1
    log.info("starter answer seed created=%s skipped=%s", created, skipped)
    return {"created": created, "skipped": skipped, "source": "starter"}


def _split_qa_candidates(text: str) -> list[tuple[str, str]]:
    """Heuristic Q&A pairs from narrative chunks (question-like headings)."""
    text = (text or "").strip()
    if len(text) < 80:
        return []
    pairs: list[tuple[str, str]] = []
    # Split on blank lines; treat a short interrogative / "Describe..." lead as Q.
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        lead = lines[0]
        body = " ".join(lines[1:]).strip()
        if len(body) < 40 or len(body) > 1800:
            continue
        looks_q = (
            lead.endswith("?")
            or re.match(
                r"^(describe|explain|provide|how|what|will you|can you|please)\b",
                lead,
                re.I,
            )
        )
        if looks_q and 12 <= len(lead) <= 280:
            pairs.append((lead.rstrip(":"), body))
    return pairs[:3]


def seed_from_won_chunks(limit: int = 80, status: str = "draft",
                         actor: str = "studio") -> dict:
    """Mine question-shaped blocks from won ITERIA narrative chunks."""
    with cursor() as cur:
        cur.execute(
            """SELECT c.chunk_id, c.module_tag, c.chunk_text, d.client_name, d.filename
               FROM harald_chunks c
               JOIN harald_documents d ON d.doc_id = c.doc_id
               WHERE d.doc_class = 'ITERIA_NARRATIVE'
                 AND NVL(d.outcome, 'unknown') IN ('won', 'in_progress', 'test')
               ORDER BY CASE d.outcome WHEN 'won' THEN 0 ELSE 1 END, c.chunk_id
               FETCH FIRST :lim ROWS ONLY""",
            {"lim": max(10, min(int(limit), 400))},
        )
        rows = cur.fetchall()

    created = 0
    skipped = 0
    for chunk_id, module, text, client, filename in rows:
        for question, answer in _split_qa_candidates(clob(text)):
            if _exists(question):
                skipped += 1
                continue
            answers.create(
                {
                    "question_canonical": question,
                    "answer_text": answer,
                    "module_tag": (module or "GENERAL"),
                    "tags": "mined,library",
                    "status": status,
                    "owner_sme": actor,
                    "source_refs": f"chunk:{chunk_id}|{client or ''}|{filename or ''}",
                },
                actor,
            )
            created += 1
    log.info("won-chunk answer seed created=%s skipped=%s scanned=%s",
             created, skipped, len(rows))
    return {"created": created, "skipped": skipped, "scanned_chunks": len(rows),
            "source": "won_chunks", "status": status}


def seed_all(actor: str = "studio") -> dict:
    starter = seed_starter(status="approved", actor=actor)
    mined = seed_from_won_chunks(limit=120, status="draft", actor=actor)
    return {"starter": starter, "mined": mined, "answers": answers.stats()}
