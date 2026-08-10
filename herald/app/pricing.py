"""Pricing file uploads. Brian's alone.

Legacy path: upload / lock a pricing spreadsheet. The fillable Studio matrix
lives in pricing_matrix.py — AI can suggest numbers from similar past matrices,
but cells stay editable and only the approver can approve / lock.
"""
from __future__ import annotations

import logging

from . import audit
from .db import connection, cursor, transaction
from .errors import Conflict, NotFound, ValidationFailed

log = logging.getLogger("harald.pricing")

STATUSES = ("draft", "final", "approved")


def upload(opp_id: int, filename: str, data: bytes, actor: str,
           notes: str | None = None) -> dict:
    """Store a new pricing version. Each upload supersedes the last."""
    if not data:
        raise ValidationFailed("The pricing file is empty.")

    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT NVL(MAX(version), 0) FROM harald_pricing WHERE opp_id = :o",
            {"o": opp_id},
        )
        previous = cur.fetchone()[0]

        cur.execute(
            "SELECT price_id FROM harald_pricing WHERE opp_id = :o AND locked = 'Y' "
            "AND version = :v",
            {"o": opp_id, "v": previous},
        )
        if cur.fetchone():
            raise Conflict(
                f"Pricing version {previous} is locked. Unlock it before uploading a "
                f"replacement."
            )

        version = previous + 1
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_pricing
                 (opp_id, version, filename, file_blob, size_bytes, status, owner, notes)
               VALUES (:opp, :ver, :fn, :blob, :size, 'draft', :owner, :notes)
               RETURNING price_id INTO :out""",
            {"opp": opp_id, "ver": version, "fn": filename, "blob": data,
             "size": len(data), "owner": actor, "notes": notes, "out": out},
        )
        price_id = out.getvalue()[0]

    audit.record(actor, "pricing.upload", "pricing", price_id,
                 {"opp_id": opp_id, "version": version, "file": filename})
    log.info("pricing uploaded opp=%s version=%s by=%s", opp_id, version, actor)
    return {"price_id": price_id, "opp_id": opp_id, "version": version,
            "filename": filename, "status": "draft", "owner": actor}


def set_status(price_id: int, status: str, actor: str) -> dict:
    if status not in STATUSES:
        raise ValidationFailed(f"status must be one of {', '.join(STATUSES)}.")
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE harald_pricing SET status = :s, locked = CASE WHEN :s2 = 'approved' "
            "THEN 'Y' ELSE locked END, updated_at = SYSTIMESTAMP WHERE price_id = :p",
            {"s": status, "s2": status, "p": price_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Pricing {price_id} not found.")
    audit.record(actor, f"pricing.{status}", "pricing", price_id)
    return get(price_id)


def set_lock(price_id: int, locked: bool, actor: str) -> dict:
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE harald_pricing SET locked = :l, updated_at = SYSTIMESTAMP "
            "WHERE price_id = :p",
            {"l": "Y" if locked else "N", "p": price_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Pricing {price_id} not found.")
    audit.record(actor, "pricing.lock" if locked else "pricing.unlock", "pricing", price_id)
    return get(price_id)


def get(price_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT price_id, opp_id, version, filename, size_bytes, status, owner,
                      locked, notes, updated_at
               FROM harald_pricing WHERE price_id = :p""",
            {"p": price_id},
        )
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Pricing {price_id} not found.")
    return {"price_id": row[0], "opp_id": row[1], "version": row[2], "filename": row[3],
            "size_bytes": row[4], "status": row[5], "owner": row[6], "locked": row[7],
            "notes": row[8], "updated_at": row[9].isoformat() if row[9] else None}


def current(opp_id: int) -> dict | None:
    """Latest pricing version for a bid, or None."""
    with cursor() as cur:
        cur.execute(
            """SELECT price_id FROM harald_pricing WHERE opp_id = :o
               ORDER BY version DESC FETCH FIRST 1 ROWS ONLY""",
            {"o": opp_id},
        )
        row = cur.fetchone()
    return get(row[0]) if row else None


def history(opp_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT price_id, version, filename, size_bytes, status, owner, locked,
                      updated_at
               FROM harald_pricing WHERE opp_id = :o ORDER BY version DESC""",
            {"o": opp_id},
        )
        return [
            {"price_id": r[0], "version": r[1], "filename": r[2], "size_bytes": r[3],
             "status": r[4], "owner": r[5], "locked": r[6],
             "updated_at": r[7].isoformat() if r[7] else None}
            for r in cur.fetchall()
        ]


def download(price_id: int) -> tuple[bytes, str]:
    with connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT file_blob, filename FROM harald_pricing WHERE price_id = :p",
                    {"p": price_id})
        row = cur.fetchone()
        if not row or row[0] is None:
            raise NotFound(f"Pricing {price_id} has no stored file.")
        blob = row[0].read() if hasattr(row[0], "read") else row[0]
        return blob, row[1]
