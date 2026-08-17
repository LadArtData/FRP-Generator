"""Review gates.

A package moves through review before it can be approved. The final gate is
reserved for the approver, which is how "Brian is the last stop" is enforced in
the workflow rather than merely agreed.
"""
from __future__ import annotations

import logging

from . import audit, packages
from .db import clob, cursor, transaction
from .errors import Conflict, Forbidden, NotFound, ValidationFailed

log = logging.getLogger("harald.reviews")

GATES = ("internal", "pink", "red", "final")
STATUSES = ("pending", "passed", "changes_requested")


def open_gate(package_id: int, gate: str, reviewer: str | None,
              actor: str | None = None) -> dict:
    if gate not in GATES:
        raise ValidationFailed(f"gate must be one of {', '.join(GATES)}.")
    packages.get(package_id)

    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT review_id FROM harald_reviews WHERE package_id = :p AND gate = :g "
            "AND status = 'pending'",
            {"p": package_id, "g": gate},
        )
        if cur.fetchone():
            raise Conflict(f"The {gate} gate is already open on this package.")
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_reviews (package_id, gate, reviewer, status)
               VALUES (:p, :g, :r, 'pending') RETURNING review_id INTO :out""",
            {"p": package_id, "g": gate, "r": reviewer, "out": out},
        )
        review_id = out.getvalue()[0]

    packages.set_status(package_id, "in_review", actor)
    audit.record(actor, "review.open", "review", review_id,
                 {"package_id": package_id, "gate": gate, "reviewer": reviewer})
    return get(review_id)


def decide(review_id: int, status: str, identity: dict, comments: str | None = None) -> dict:
    if status not in ("passed", "changes_requested"):
        raise ValidationFailed("A decision must be passed or changes_requested.")

    review = get(review_id)
    if review["status"] != "pending":
        raise Conflict("This gate has already been decided.")
    if review["gate"] == "final" and identity.get("role") != "approver":
        raise Forbidden(
            "The final gate can only be decided by the approver.",
            {"required": "approver", "actual": identity.get("role")},
        )

    with transaction() as conn:
        conn.cursor().execute(
            """UPDATE harald_reviews
               SET status = :s, comments = :c, reviewer = NVL(reviewer, :actor),
                   decided_at = SYSTIMESTAMP
               WHERE review_id = :r""",
            {"s": status, "c": comments, "actor": identity["username"], "r": review_id},
        )

    if status == "changes_requested":
        packages.set_status(review["package_id"], "draft", identity["username"])

    audit.record(identity["username"], f"review.{status}", "review", review_id,
                 {"package_id": review["package_id"], "gate": review["gate"]})
    return get(review_id)


def get(review_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT review_id, package_id, gate, reviewer, status, comments,
                      created_at, decided_at
               FROM harald_reviews WHERE review_id = :r""",
            {"r": review_id},
        )
        row = cur.fetchone()
        comments = clob(row[5]) if row else ""
    if not row:
        raise NotFound(f"Review {review_id} not found.")
    return {"review_id": row[0], "package_id": row[1], "gate": row[2], "reviewer": row[3],
            "status": row[4], "comments": comments,
            "created_at": row[6].isoformat() if row[6] else None,
            "decided_at": row[7].isoformat() if row[7] else None}


def for_package(package_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT review_id, gate, reviewer, status, comments, created_at, decided_at
               FROM harald_reviews WHERE package_id = :p ORDER BY created_at""",
            {"p": package_id},
        )
        return [
            {"review_id": r[0], "gate": r[1], "reviewer": r[2], "status": r[3],
             "comments": clob(r[4]),
             "created_at": r[5].isoformat() if r[5] else None,
             "decided_at": r[6].isoformat() if r[6] else None}
            for r in cur.fetchall()
        ]
