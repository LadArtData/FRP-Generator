"""Document store and the ingestion pipeline.

Every file lives in harald_documents. Classification decides what happens next:
only ITERIA_NARRATIVE is chunked and embedded into the retrieval index. Amendments
are stored as new versions that supersede the prior file, so a bid's document set
is complete and current when it is recalled.
"""
from __future__ import annotations

import hashlib
import logging
import os

from . import chunking, classifier, embeddings
from .db import clob, connection, cursor, transaction
from .errors import NotFound, ValidationFailed

log = logging.getLogger("harald.documents")


def store(filename: str, data: bytes, *, opp_id: int | None = None,
          doc_role: str = "reference", doc_class: str | None = None,
          client_name: str | None = None, state: str | None = None,
          outcome: str = "in_progress", effective_date: str | None = None,
          actor: str | None = None, source_path: str | None = None) -> dict:
    """Store a document, classify it, and index it if it is iteria narrative."""
    if not data:
        raise ValidationFailed(f"{filename} is empty.")

    resolved_class = doc_class or classifier.classify_path(source_path or filename, filename)
    text = ""
    blocks: list[tuple[str, str]] = []
    try:
        blocks = chunking.extract(filename, data)
        text = chunking.plain_text(blocks)
    except ValidationFailed:
        # Forms and scans that cannot be parsed are still valid attachments; they
        # simply carry no extractable text and are never indexed.
        log.info("no extractable text from %s; storing as attachment", filename)

    digest = hashlib.sha256(data).hexdigest()

    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT doc_id, version FROM harald_documents
               WHERE NVL(opp_id, -1) = NVL(:opp, -1) AND filename = :fn AND doc_role = :role
               ORDER BY version DESC FETCH FIRST 1 ROWS ONLY""",
            {"opp": opp_id, "fn": filename, "role": doc_role},
        )
        previous = cur.fetchone()
        version = (previous[1] + 1) if previous else 1
        supersedes = previous[0] if previous else None

        doc_id_var = cur.var(int)
        cur.execute(
            """INSERT INTO harald_documents
                 (opp_id, filename, doc_class, doc_role, client_name, state, outcome,
                  version, effective_date, supersedes_id, file_blob, size_bytes,
                  sha256, doc_text, uploaded_by)
               VALUES (:opp, :fn, :cls, :role, :client, :state, :outcome, :ver, :eff,
                       :sup, :blob, :size, :sha, :text, :actor)
               RETURNING doc_id INTO :out""",
            {"opp": opp_id, "fn": filename, "cls": resolved_class, "role": doc_role,
             "client": client_name, "state": state, "outcome": outcome, "ver": version,
             "eff": effective_date, "sup": supersedes, "blob": data, "size": len(data),
             "sha": digest, "text": text, "actor": actor, "out": doc_id_var},
        )
        doc_id = doc_id_var.getvalue()[0]

    indexed = 0
    if resolved_class == classifier.ITERIA_NARRATIVE and blocks:
        indexed = index_document(doc_id, blocks)

    log.info("stored doc_id=%s file=%s class=%s role=%s chunks=%s",
             doc_id, filename, resolved_class, doc_role, indexed)
    return {"doc_id": doc_id, "filename": filename, "doc_class": resolved_class,
            "doc_role": doc_role, "version": version, "supersedes_id": supersedes,
            "size_bytes": len(data), "chunks": indexed}


def index_document(doc_id: int, blocks: list[tuple[str, str]] | None = None) -> int:
    """Chunk, embed, and index a narrative document. Replaces any prior chunks so
    re-indexing is idempotent."""
    if blocks is None:
        text_blocks = get_text(doc_id)
        if not text_blocks:
            return 0
        blocks = [("P", line) for line in text_blocks.split("\n") if line.strip()]

    pieces = chunking.chunk(blocks)
    if not pieces:
        return 0

    vectors = embeddings.embed_passages([p["text"] for p in pieces])
    rows = [
        {"doc": doc_id, "mod": piece["module"], "sec": piece["section"], "idx": index,
         "text": piece["text"], "tok": piece["token_count"],
         "src": piece["tag_source"], "vec": vector}
        for index, (piece, vector) in enumerate(zip(pieces, vectors))
    ]

    with transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM harald_chunks WHERE doc_id = :d", {"d": doc_id})
        cur.executemany(
            """INSERT INTO harald_chunks
                 (doc_id, module_tag, section_tag, chunk_index, chunk_text,
                  token_count, tag_source, embedding)
               VALUES (:doc, :mod, :sec, :idx, :text, :tok, :src, :vec)""",
            rows,
        )
        cur.execute(
            "UPDATE harald_documents SET promoted_to_lib = 'Y', doc_class = 'ITERIA_NARRATIVE' "
            "WHERE doc_id = :d",
            {"d": doc_id},
        )
    return len(rows)


def promote(doc_id: int, *, client_name: str | None = None,
            outcome: str = "won") -> dict:
    """Promote a finished iteria proposal into the library. This is the compounding
    loop: a won bid becomes retrievable source for the next one."""
    meta = get(doc_id)
    with transaction() as conn:
        conn.cursor().execute(
            """UPDATE harald_documents
               SET doc_class = 'ITERIA_NARRATIVE', outcome = :outcome,
                   client_name = NVL(:client, client_name)
               WHERE doc_id = :d""",
            {"outcome": outcome, "client": client_name, "d": doc_id},
        )
    text = get_text(doc_id)
    if not text.strip():
        raise ValidationFailed(
            f"{meta['filename']} has no extractable text, so it cannot be added to the library."
        )
    blob = get_blob(doc_id)[0]
    blocks = chunking.extract(meta["filename"], blob)
    chunks = index_document(doc_id, blocks)
    return {"doc_id": doc_id, "filename": meta["filename"], "chunks": chunks,
            "outcome": outcome, "promoted": True}


def get(doc_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT d.doc_id, d.opp_id, d.filename, d.doc_class, d.doc_role, d.client_name,
                      d.state, d.outcome, d.version, d.effective_date, d.supersedes_id,
                      d.size_bytes, d.promoted_to_lib, d.uploaded_at,
                      (SELECT COUNT(*) FROM harald_chunks c WHERE c.doc_id = d.doc_id)
               FROM harald_documents d WHERE d.doc_id = :d""",
            {"d": doc_id},
        )
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Document {doc_id} not found.")
    return {"doc_id": row[0], "opp_id": row[1], "filename": row[2], "doc_class": row[3],
            "doc_role": row[4], "client_name": row[5], "state": row[6], "outcome": row[7],
            "version": row[8], "effective_date": row[9], "supersedes_id": row[10],
            "size_bytes": row[11], "promoted_to_lib": row[12],
            "uploaded_at": row[13].isoformat() if row[13] else None, "chunk_count": row[14]}


def get_text(doc_id: int) -> str:
    with cursor() as cur:
        cur.execute("SELECT doc_text FROM harald_documents WHERE doc_id = :d", {"d": doc_id})
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Document {doc_id} not found.")
    return clob(row[0])


def get_blob(doc_id: int) -> tuple[bytes, str]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT file_blob, filename FROM harald_documents WHERE doc_id = :d",
                    {"d": doc_id})
        row = cur.fetchone()
        if not row or row[0] is None:
            raise NotFound(f"Document {doc_id} has no stored file.")
        blob = row[0].read() if hasattr(row[0], "read") else row[0]
        return blob, row[1]


def list_documents(*, opp_id: int | None = None, doc_class: str | None = None,
                   outcome: str | None = None, query: str | None = None,
                   library_only: bool = False, limit: int = 300) -> list[dict]:
    where = ["1 = 1"]
    binds: dict = {"lim": limit}
    if opp_id is not None:
        where.append("d.opp_id = :opp")
        binds["opp"] = opp_id
    if library_only:
        where.append("d.doc_class = 'ITERIA_NARRATIVE'")
    if doc_class:
        where.append("d.doc_class = :cls")
        binds["cls"] = doc_class
    if outcome:
        where.append("d.outcome = :outcome")
        binds["outcome"] = outcome
    if query:
        where.append("LOWER(d.filename) LIKE :q")
        binds["q"] = f"%{query.lower()}%"

    sql = f"""SELECT * FROM (
                SELECT d.doc_id, d.opp_id, d.filename, d.doc_class, d.doc_role, d.client_name,
                       d.outcome, d.size_bytes, d.version, d.promoted_to_lib, d.uploaded_at,
                       (SELECT COUNT(*) FROM harald_chunks c WHERE c.doc_id = d.doc_id) chunks
                FROM harald_documents d
                WHERE {' AND '.join(where)}
                ORDER BY d.uploaded_at DESC
              ) WHERE ROWNUM <= :lim"""
    with cursor() as cur:
        cur.execute(sql, binds)
        return [
            {"doc_id": r[0], "opp_id": r[1], "filename": r[2], "doc_class": r[3],
             "doc_role": r[4], "client_name": r[5], "deal_status": r[6], "size_bytes": r[7],
             "version": r[8], "promoted_to_lib": r[9],
             "uploaded_at": r[10].isoformat() if r[10] else None, "chunk_count": r[11]}
            for r in cur.fetchall()
        ]


def library_stats() -> dict:
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM harald_documents")
        total_docs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM harald_documents WHERE doc_class = 'ITERIA_NARRATIVE'")
        narrative_docs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM harald_chunks")
        total_chunks = cur.fetchone()[0]
        cur.execute(
            "SELECT LOWER(NVL(outcome, 'unknown')), COUNT(*) FROM harald_documents "
            "WHERE doc_class = 'ITERIA_NARRATIVE' GROUP BY LOWER(NVL(outcome, 'unknown'))"
        )
        by_status = [{"status": r[0], "count": r[1]} for r in cur.fetchall()]
        cur.execute("SELECT module_tag, COUNT(*) FROM harald_chunks GROUP BY module_tag "
                    "ORDER BY COUNT(*) DESC")
        by_module = [{"module": r[0], "count": r[1]} for r in cur.fetchall()]
    return {"total_docs": total_docs, "narrative_docs": narrative_docs,
            "total_chunks": total_chunks, "by_status": by_status, "by_module": by_module}
