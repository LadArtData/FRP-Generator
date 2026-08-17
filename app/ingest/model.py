"""Parse User & Role Access Audit Report exports."""

from __future__ import annotations

import csv
import io
import logging
from typing import BinaryIO

from ..detection.models import ModelRow

log = logging.getLogger("warden.ingest.model")

ROLE_COLS = ("role code", "role_code", "role name", "role")
PRIV_COLS = ("privilege code", "privilege_code", "privilege")
PERSON_COLS = ("user name", "username", "person reference", "person_ref")
SCOPE_COLS = ("data role scope", "scope", "data security scope")


def _pick(row: dict, candidates: tuple[str, ...]) -> str:
    norm = {k.strip().lower(): v for k, v in row.items()}
    for c in candidates:
        if c in norm and norm[c]:
            return str(norm[c]).strip()
    return ""


def parse_model_csv(source: BinaryIO | str) -> list[ModelRow]:
    """Normalize a security model export into HOLDER / ROLE / GRANT rows."""
    if isinstance(source, (bytes, bytearray)):
        text = source.decode("utf-8-sig", errors="replace")
    else:
        text = source.read() if hasattr(source, "read") else str(source)

    reader = csv.DictReader(io.StringIO(text))
    rows: list[ModelRow] = []
    for raw in reader:
        person = _pick(raw, PERSON_COLS)
        role = _pick(raw, ROLE_COLS)
        priv = _pick(raw, PRIV_COLS)
        scope = _pick(raw, SCOPE_COLS).lower()

        if role and not priv and not person:
            rows.append(ModelRow("ROLE", role, attrs={
                "name": _pick(raw, ("role name", "role_name")) or role,
                "area": _pick(raw, ("module", "area", "application")),
                "scope": scope or "scoped",
            }))
        elif role and priv:
            rows.append(ModelRow("GRANT", priv, parent_key=role))
            rows.append(ModelRow("PRIV", priv, attrs={
                "name": _pick(raw, ("privilege name", "privilege_name")) or priv,
                "area": _pick(raw, ("module", "area")),
            }))
        elif person and role:
            rows.append(ModelRow("HOLDER", role, parent_key=person))

    log.info("parsed %d model rows", len(rows))
    return rows
