"""Append-only audit trail. Every state change records who did what, to what."""
from __future__ import annotations

import json
import logging

from .db import transaction, cursor

log = logging.getLogger("harald.audit")


def record(actor: str | None, action: str, entity_type: str,
           entity_id: int | None = None, detail: dict | None = None) -> None:
    payload = json.dumps(detail, default=str)[:4000] if detail else None
    try:
        with transaction() as conn:
            conn.cursor().execute(
                """INSERT INTO harald_audit (actor, action, entity_type, entity_id, detail)
                   VALUES (:actor, :action, :etype, :eid, :detail)""",
                {"actor": actor or "system", "action": action, "etype": entity_type,
                 "eid": entity_id, "detail": payload},
            )
    except Exception:
        # An audit failure must never break the operation it is recording.
        log.exception("audit write failed action=%s entity=%s/%s", action, entity_type, entity_id)


def trail(entity_type: str | None = None, entity_id: int | None = None,
          limit: int = 100) -> list[dict]:
    where, binds = "1=1", {"lim": limit}
    if entity_type:
        where += " AND entity_type = :etype"
        binds["etype"] = entity_type
    if entity_id is not None:
        where += " AND entity_id = :eid"
        binds["eid"] = entity_id
    sql = f"""SELECT * FROM (
                SELECT event_id, actor, action, entity_type, entity_id, detail, at
                FROM harald_audit WHERE {where} ORDER BY event_id DESC
              ) WHERE ROWNUM <= :lim"""
    with cursor() as cur:
        cur.execute(sql, binds)
        return [
            {"event_id": r[0], "actor": r[1], "action": r[2], "entity_type": r[3],
             "entity_id": r[4], "detail": r[5], "at": r[6].isoformat() if r[6] else None}
            for r in cur.fetchall()
        ]
