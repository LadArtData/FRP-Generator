"""Retrieval.

Semantic vector search in Oracle, hard-filtered to iteria's own narrative and
weighted by outcome so winning language surfaces first. A competitor's proposal
or the client's own RFP can never be returned as answer content, because only
ITERIA_NARRATIVE documents are indexed and the join re-asserts that filter.
"""
from __future__ import annotations

import logging

from . import embeddings
from .config import cfg
from .db import clob, cursor

log = logging.getLogger("harald.retrieval")

# Cosine distance multipliers. Lower is better, so a win is discounted (favoured)
# and a loss is penalised. A lost bid still holds usable language, so it is
# demoted rather than excluded.
_OUTCOME_WEIGHT = {"won": 0.78, "in_progress": 1.00, "test": 0.92,
                   "lost": 1.28, "no_bid": 1.35}


def retrieve(query: str, module: str | None = None, k: int | None = None,
             section: str | None = None) -> list[dict]:
    if not query or not query.strip():
        return []
    k = k or cfg.top_k
    vector = embeddings.embed_query(query)

    sql = """
        SELECT * FROM (
          SELECT c.chunk_id, d.client_name, d.outcome, c.module_tag, c.section_tag,
                 VECTOR_DISTANCE(c.embedding, :qvec, COSINE) *
                   CASE d.outcome
                     WHEN 'won'         THEN 0.78
                     WHEN 'in_progress' THEN 1.00
                     WHEN 'test'        THEN 0.92
                     WHEN 'lost'        THEN 1.28
                     ELSE 1.35
                   END *
                   CASE WHEN :module IS NULL THEN 1.0
                        WHEN c.module_tag = :module THEN 0.80
                        WHEN c.module_tag = 'GENERAL' THEN 1.05
                        ELSE 1.30
                   END AS weighted_distance,
                 c.chunk_text
          FROM   harald_chunks c
          JOIN   harald_documents d ON d.doc_id = c.doc_id
          WHERE  d.doc_class = 'ITERIA_NARRATIVE'
          AND    (:section IS NULL OR c.section_tag = :section)
          ORDER  BY weighted_distance
        ) WHERE ROWNUM <= :k
    """
    binds = {"qvec": vector, "module": module.upper() if module else None,
             "section": section.lower() if section else None, "k": k}

    with cursor() as cur:
        cur.execute(sql, binds)
        results = [
            {"chunk_id": r[0], "client": r[1], "outcome": r[2], "module": r[3],
             "section": r[4], "score": round(float(r[5]), 4), "text": clob(r[6])}
            for r in cur.fetchall()
        ]
    log.debug("retrieved %s chunks module=%s", len(results), module)
    return results
