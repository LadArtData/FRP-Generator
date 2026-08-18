"""Pricing matrix: line sets must match what the bid actually is, and the CLOB
read must happen before the connection goes back to the pool.

Both of these were defects I shipped. The line-set one was visible on screen —
Jefferson County Sheriff's Office priced with "Payroll configuration". The CLOB
one was invisible only because no bid had a saved matrix yet.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import pricing_matrix as pm  # noqa: E402


def _items(rows):
    return {r["line_item"].strip().lower() for r in rows}


# --- line sets --------------------------------------------------------------

def test_consulting_lines_name_no_erp_modules():
    """A sheriff's office consulting bid must not be priced with ERP config."""
    banned = ("financials configuration", "hcm configuration", "payroll configuration",
              "procurement / p2p configuration", "budget / epm configuration")
    items = _items(pm.CONSULTING_LINES)
    for b in banned:
        assert b not in items, f"{b} has no place on a consulting engagement"


def test_consulting_lines_cover_real_consulting_work():
    items = _items(pm.CONSULTING_LINES)
    for expected in ("project management", "training & knowledge transfer",
                     "travel & expenses", "contingency"):
        assert expected in items
    # The work that distinguishes advisory from delivery.
    assert any("assessment" in i for i in items)
    assert any("process" in i for i in items)


def test_every_line_set_is_shaped_like_a_matrix_row():
    required = {"category", "line_item", "unit", "qty", "rate", "amount",
                "notes", "ai_suggested"}
    for name, rows in (("DEFAULT", pm.DEFAULT_LINES),
                       ("AI_ENABLEMENT", pm.AI_ENABLEMENT_LINES),
                       ("CONSULTING", pm.CONSULTING_LINES)):
        assert rows, name
        for row in rows:
            assert set(row) == required, f"{name}: {row.get('line_item')}"
            assert row["line_item"].strip(), name
            assert row["unit"] in ("hours", "lump"), name


def test_line_sets_have_no_duplicate_items():
    for name, rows in (("DEFAULT", pm.DEFAULT_LINES),
                       ("AI_ENABLEMENT", pm.AI_ENABLEMENT_LINES),
                       ("CONSULTING", pm.CONSULTING_LINES)):
        items = [r["line_item"].strip().lower() for r in rows]
        assert len(items) == len(set(items)), f"{name} has duplicate line items"


def test_normalize_accepts_every_line_set():
    """_normalize_lines must round-trip each set without silently dropping rows."""
    for rows in (pm.DEFAULT_LINES, pm.AI_ENABLEMENT_LINES, pm.CONSULTING_LINES):
        out = pm._normalize_lines([dict(r) for r in rows])
        assert len(out) == len(rows)
        assert _items(out) == _items(rows)


# --- CLOB lifetime ----------------------------------------------------------

def test_saved_matrix_is_read_inside_the_cursor_block():
    """_row_to_dict reads two CLOB locators; it must be called before the
    `with cursor()` block exits, or a saved matrix 500s on reload."""
    src = inspect.getsource(pm.get_for_opportunity)
    body = src.split("with cursor() as cur:", 1)[1]
    inside, _, after = body.partition("\n    if saved is not None:")
    assert "_row_to_dict(row)" in inside, (
        "_row_to_dict must run inside the cursor block — it reads lines_json "
        "and suggested_from, which are CLOB locators tied to the connection."
    )
    assert "_row_to_dict" not in after, "no LOB read may happen after the block"


def test_row_to_dict_is_the_only_clob_reader_and_stays_put():
    """Guard against a future edit reintroducing a read after the block."""
    src = inspect.getsource(pm)
    for fn_name in ("get_for_opportunity", "suggest"):
        fn_src = inspect.getsource(getattr(pm, fn_name))
        for match in re.finditer(r"_row_to_dict", fn_src):
            before = fn_src[:match.start()]
            assert "with cursor() as cur:" in before, (
                f"{fn_name}: _row_to_dict called outside a cursor block"
            )
