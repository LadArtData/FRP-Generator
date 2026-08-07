"""Per-agency submission format profiles.

Every agency mandates its own layout: page order, heading scheme, page limits, and
the forms that must accompany the response. A profile captures that once and drives
package assembly, so a bid is submitted in the agency's required shape rather than
in whatever shape the drafting happened to produce.
"""
from __future__ import annotations

import json
import logging

from .db import clob, cursor, transaction
from .errors import NotFound, ValidationFailed

log = logging.getLogger("harald.formats")

# Sections whose body is drawn from somewhere other than a generated narrative.
SOURCE_KINDS = ("generated", "requirements", "pricing", "form", "manual")


def _json_field(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(clob(value))
    except (json.JSONDecodeError, TypeError):
        return default


def list_profiles() -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT profile_id, name, agency, font_name, font_size, margin_inches,
                      cover_required, toc_required, notes, page_order
               FROM harald_format_profiles ORDER BY name"""
        )
        return [
            {"profile_id": r[0], "name": r[1], "agency": r[2], "font_name": r[3],
             "font_size": r[4], "margin_inches": r[5], "cover_required": r[6],
             "toc_required": r[7], "notes": r[8],
             "section_count": len(_json_field(r[9], []))}
            for r in cur.fetchall()
        ]


def get(profile_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            """SELECT profile_id, name, agency, page_order, heading_scheme, page_limits,
                      required_forms, font_name, font_size, margin_inches,
                      cover_required, toc_required, notes
               FROM harald_format_profiles WHERE profile_id = :p""",
            {"p": profile_id},
        )
        row = cur.fetchone()
    if not row:
        raise NotFound(f"Format profile {profile_id} not found.")
    return {
        "profile_id": row[0], "name": row[1], "agency": row[2],
        "page_order": _json_field(row[3], []),
        "heading_scheme": _json_field(row[4], {"numbered": True, "style": "decimal"}),
        "page_limits": _json_field(row[5], {}),
        "required_forms": _json_field(row[6], []),
        "font_name": row[7] or "Calibri", "font_size": row[8] or 11,
        "margin_inches": row[9] or 1, "cover_required": row[10] or "Y",
        "toc_required": row[11] or "Y", "notes": row[12],
    }


def default_profile() -> dict:
    with cursor() as cur:
        cur.execute(
            "SELECT profile_id FROM harald_format_profiles ORDER BY profile_id "
            "FETCH FIRST 1 ROWS ONLY"
        )
        row = cur.fetchone()
    if not row:
        raise NotFound("No format profile exists. Create one before assembling a package.")
    return get(row[0])


def _validate_page_order(page_order) -> str:
    if not isinstance(page_order, list) or not page_order:
        raise ValidationFailed("page_order must be a non-empty list of sections.")
    for entry in page_order:
        if not isinstance(entry, dict) or not entry.get("title"):
            raise ValidationFailed("Each page_order entry needs a title.")
        source = entry.get("source", "generated")
        if source not in SOURCE_KINDS:
            raise ValidationFailed(
                f"Unknown section source {source!r}. Use one of: {', '.join(SOURCE_KINDS)}."
            )
        entry.setdefault("key", entry["title"].lower().replace(" ", "_")[:40])
        entry.setdefault("source", source)
    return json.dumps(page_order)


def create(payload: dict, actor: str | None = None) -> int:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationFailed("A profile name is required.")
    page_order = _validate_page_order(payload.get("page_order"))

    with transaction() as conn:
        cur = conn.cursor()
        out = cur.var(int)
        cur.execute(
            """INSERT INTO harald_format_profiles
                 (name, agency, page_order, heading_scheme, page_limits, required_forms,
                  font_name, font_size, margin_inches, cover_required, toc_required, notes)
               VALUES (:name, :agency, :order, :heading, :limits, :forms, :font, :size,
                       :margin, :cover, :toc, :notes)
               RETURNING profile_id INTO :out""",
            {"name": name, "agency": payload.get("agency"), "order": page_order,
             "heading": json.dumps(payload.get("heading_scheme")
                                   or {"numbered": True, "style": "decimal"}),
             "limits": json.dumps(payload.get("page_limits") or {}),
             "forms": json.dumps(payload.get("required_forms") or []),
             "font": payload.get("font_name") or "Calibri",
             "size": payload.get("font_size") or 11,
             "margin": payload.get("margin_inches") or 1,
             "cover": "Y" if payload.get("cover_required", True) else "N",
             "toc": "Y" if payload.get("toc_required", True) else "N",
             "notes": payload.get("notes"), "out": out},
        )
        return out.getvalue()[0]


def update(profile_id: int, payload: dict) -> None:
    columns: dict = {}
    for key in ("name", "agency", "font_name", "font_size", "margin_inches", "notes"):
        if key in payload:
            columns[key] = payload[key]
    if "page_order" in payload:
        columns["page_order"] = _validate_page_order(payload["page_order"])
    if "heading_scheme" in payload:
        columns["heading_scheme"] = json.dumps(payload["heading_scheme"])
    if "page_limits" in payload:
        columns["page_limits"] = json.dumps(payload["page_limits"])
    if "required_forms" in payload:
        columns["required_forms"] = json.dumps(payload["required_forms"])
    if "cover_required" in payload:
        columns["cover_required"] = "Y" if payload["cover_required"] else "N"
    if "toc_required" in payload:
        columns["toc_required"] = "Y" if payload["toc_required"] else "N"
    if not columns:
        return

    with transaction() as conn:
        cur = conn.cursor()
        assignments = ", ".join(f"{col} = :{col}" for col in columns)
        cur.execute(
            f"UPDATE harald_format_profiles SET {assignments}, updated_at = SYSTIMESTAMP "
            f"WHERE profile_id = :profile_id",
            {**columns, "profile_id": profile_id},
        )
        if cur.rowcount == 0:
            raise NotFound(f"Format profile {profile_id} not found.")


def clone(profile_id: int, name: str, agency: str | None = None) -> int:
    source = get(profile_id)
    source.update({"name": name, "agency": agency or source["agency"]})
    source.pop("profile_id")
    return create(source)
