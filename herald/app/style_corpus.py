"""Style anchor corpus — rhythm and calibration from harald_chunks, not hardcoded prose.

Documents marked style_anchor=Y (or St. Petersburg by filename fallback) supply
the voice target. BANNED vocabulary stays in voice.py as structured rules.
"""
from __future__ import annotations

import logging
import threading

from . import voice
from .db import clob, cursor

log = logging.getLogger("harald.style_corpus")

_lock = threading.Lock()
_rhythm: dict | None = None
_calibration: dict | None = None
_anchor_excerpts: list[str] = []


def _anchor_sql(use_column: bool) -> str:
    anchor_filter = "d.style_anchor = 'Y'" if use_column else (
        "(LOWER(d.filename) LIKE '%stpetersburg%' "
        "OR LOWER(d.client_name) LIKE '%st. petersburg%')"
    )
    return f"""
        SELECT c.chunk_text, d.filename, d.client_name
        FROM   harald_chunks c
        JOIN   harald_documents d ON d.doc_id = c.doc_id
        WHERE  d.doc_class = 'ITERIA_NARRATIVE'
        AND    {anchor_filter}
        ORDER  BY c.chunk_index
    """


def fetch_anchor_rows(limit: int = 999) -> list[dict]:
    """Return style-anchor chunk rows, or [] when the table is empty / unavailable."""
    for use_column in (True, False):
        try:
            with cursor() as cur:
                cur.execute(_anchor_sql(use_column))
                rows = cur.fetchall()
            if rows:
                return [
                    {"text": clob(r[0]), "filename": r[1] or "", "client": r[2] or ""}
                    for r in rows[:limit]
                ]
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if use_column and ("style_anchor" in err or "ORA-00904" in err):
                continue
            log.warning("style anchor query failed: %s", exc)
            return []
    return []


def refresh() -> None:
    """Load rhythm thresholds and prompt excerpts from the style anchor corpus."""
    global _rhythm, _calibration, _anchor_excerpts
    rows = fetch_anchor_rows()
    texts = [r["text"] for r in rows if (r.get("text") or "").strip()]
    excerpts = voice.pick_voice_exemplars(
        [{"text": t, "trust_level": "VERIFIED"} for t in texts],
        n=3,
        prefer_verified=False,
    )

    rhythm = voice.DEFAULT_RHYTHM
    calibration = dict(voice.DEFAULT_CALIBRATION)
    if texts:
        source = rows[0]
        calibration["source"] = (
            f"{source.get('client') or source.get('filename') or 'style anchor'}, "
            "retrieval_tier=CANONICAL, style_anchor=Y"
        )
        try:
            measured = voice.calibrate_from_anchor(texts)
            rhythm = measured["RHYTHM"]
            calibration["measured"] = measured["measured"]
            calibration["words"] = measured["measured"]["words"]
            calibration["sentences"] = measured["measured"]["sentences"]
            log.info(
                "style anchor loaded chunks=%s words=%s sentences=%s",
                len(texts),
                calibration["words"],
                calibration["sentences"],
            )
        except ValueError as exc:
            log.warning("style anchor too small to calibrate (%s); using defaults", exc)
    else:
        log.info("no style anchor in database; using built-in rhythm defaults")

    with _lock:
        _rhythm = rhythm
        _calibration = calibration
        _anchor_excerpts = excerpts


def get_rhythm() -> dict:
    with _lock:
        return dict(_rhythm or voice.DEFAULT_RHYTHM)


def get_calibration() -> dict:
    with _lock:
        return dict(_calibration or voice.DEFAULT_CALIBRATION)


def anchor_excerpts() -> list[str]:
    with _lock:
        return list(_anchor_excerpts)


def anchor_excerpt_block() -> str:
    bits = anchor_excerpts()
    if not bits:
        return ""
    joined = "\n\n---\n\n".join(bits[:2])
    return (
        "\n\nSTYLE ANCHOR (match this cadence; do not copy facts verbatim):\n"
        f"{joined}"
    )
