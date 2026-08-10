"""Application identity and roles.

The Oracle database has a single shared ADMIN login, so HARALD cannot derive
who is acting from the database session. It keeps its own identity layer: a
signed, expiring session token issued at sign-in and required on every
state-changing call.

Roles
  contributor  draft, edit, import, fill
  reviewer     the above, plus review gates other than final
  approver     Brian. The only role that may upload or lock pricing, decide the
               final gate, approve a package, or mark it submitted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from .config import cfg
from .db import cursor
from .errors import Forbidden, Unauthorized

log = logging.getLogger("harald.auth")

CONTRIBUTOR, REVIEWER, APPROVER = "contributor", "reviewer", "approver"
_RANK = {CONTRIBUTOR: 1, REVIEWER: 2, APPROVER: 3}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(cfg.session_secret.encode(), payload, hashlib.sha256).digest())


def issue_token(username: str, role: str) -> str:
    body = json.dumps(
        {"u": username, "r": role, "exp": int(time.time()) + cfg.session_hours * 3600},
        separators=(",", ":"),
    ).encode()
    return f"{_b64e(body)}.{_sign(body)}"


def parse_token(token: str | None) -> dict:
    if not token:
        raise Unauthorized("Sign in required.")
    try:
        body_b64, sig = token.split(".", 1)
        body = _b64d(body_b64)
    except Exception as exc:
        raise Unauthorized("Malformed session token.") from exc
    if not hmac.compare_digest(sig, _sign(body)):
        raise Unauthorized("Invalid session token.")
    claims = json.loads(body)
    if claims.get("exp", 0) < time.time():
        raise Unauthorized("Session expired. Sign in again.")
    return {"username": claims["u"], "role": claims["r"]}


def list_users() -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT username, display_name, role FROM harald_users
               WHERE active = 'Y' ORDER BY DECODE(role,'approver',1,'reviewer',2,3), username"""
        )
        return [{"username": r[0], "display_name": r[1], "role": r[2]} for r in cur.fetchall()]


def sign_in(username: str, passphrase: str | None = None) -> dict:
    """Pick a name from the roster. No passphrase.

    The Oracle ADMIN login is shared; HARALD's own role on that name is what
    gates pricing and final approval. A second secret nobody will remember was
    just friction, so it is gone. `passphrase` is accepted and ignored so old
    clients do not break.
    """
    _ = passphrase
    with cursor() as cur:
        cur.execute(
            "SELECT username, display_name, role FROM harald_users "
            "WHERE LOWER(username) = LOWER(:u) AND active = 'Y'",
            {"u": (username or "").strip()},
        )
        row = cur.fetchone()
    if not row:
        raise Unauthorized("Unknown user.")
    uname, display, role = row
    log.info("sign-in username=%s role=%s", uname, role)
    return {
        "token": issue_token(uname, role),
        "username": uname,
        "display_name": display,
        "role": role,
    }


def require(identity: dict, minimum: str) -> dict:
    if _RANK.get(identity.get("role"), 0) < _RANK[minimum]:
        raise Forbidden(
            f"This action requires the {minimum} role.",
            {"required": minimum, "actual": identity.get("role")},
        )
    return identity


def require_approver(identity: dict) -> dict:
    """Pricing and the final gate. Brian only."""
    if identity.get("role") != APPROVER:
        raise Forbidden(
            "Pricing and final approval are restricted to the approver.",
            {"required": APPROVER, "actual": identity.get("role")},
        )
    return identity
