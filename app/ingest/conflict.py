"""Parse Oracle Fusion SOD conflict disposition exports."""

from __future__ import annotations

import csv
import io
import logging
from typing import BinaryIO

from ..detection.models import ConflictRow

log = logging.getLogger("warden.ingest.conflict")

# Column names vary by extract; map normalized keys to accepted headers.
PERSON_COLS = ("person reference", "user name", "username", "person_ref", "reference")
RULE_COLS = ("rule id", "rule_id", "sod rule", "rule")
UNIT_COLS = ("unit", "department", "school", "organization", "area of responsibility")


def _pick(row: dict, candidates: tuple[str, ...]) -> str:
    norm = {k.strip().lower(): v for k, v in row.items()}
    for c in candidates:
        if c in norm and norm[c]:
            return str(norm[c]).strip()
    return ""


def parse_conflict_csv(source: BinaryIO | str) -> list[ConflictRow]:
    """Load conflict analysis rows from CSV text or bytes."""
    if isinstance(source, (bytes, bytearray)):
        text = source.decode("utf-8-sig", errors="replace")
    else:
        text = source.read() if hasattr(source, "read") else str(source)

    reader = csv.DictReader(io.StringIO(text))
    rows: list[ConflictRow] = []
    for raw in reader:
        person = _pick(raw, PERSON_COLS)
        rule = _pick(raw, RULE_COLS)
        if not person or not rule:
            continue
        rows.append(ConflictRow(
            person_ref=person,
            rule_id=rule.upper(),
            unit=_pick(raw, UNIT_COLS) or "Unattributed",
            disposition=_pick(raw, ("disposition", "status", "result")),
        ))
    log.info("parsed %d conflict rows", len(rows))
    return rows
