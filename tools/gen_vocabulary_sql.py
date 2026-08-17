"""Write the vocabulary CHECK constraints into schema/harald_schema.sql from
app/vocabulary.py, so the SQL and the Python cannot disagree.

Run after changing a vocabulary:

    python tools/gen_vocabulary_sql.py

The block it maintains is delimited by BEGIN/END markers; everything outside
them is left alone. tests/test_logic.py re-derives the same text and fails if
the file is stale, so a forgotten run is caught before it reaches the database.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.vocabulary import (MODULES, SECTIONS, TAG_SOURCES, TRUST_LEVELS,  # noqa: E402
                            sql_in_list)

BEGIN = "-- BEGIN GENERATED VOCABULARY (tools/gen_vocabulary_sql.py) --"
END = "-- END GENERATED VOCABULARY --"

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema" / "harald_schema.sql"


def block() -> str:
    """The constraint text. Applied with ALTER TABLE rather than inline so the
    generated region is one contiguous block instead of edits scattered through
    three CREATE TABLE statements."""
    return "\n".join([
        BEGIN,
        "-- Generated from app/vocabulary.py. Do not hand-edit: run the generator.",
        "-- These constraints are the reason a tag cannot silently drift out of",
        "-- range and return an empty library with no error.",
        "",
        "ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_section_ck",
        "  CHECK (section_tag IN (",
        sql_in_list(SECTIONS),
        "  ));",
        "",
        "ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_module_ck",
        "  CHECK (module_tag IN (",
        sql_in_list(MODULES),
        "  ));",
        "",
        "ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_tagsrc_ck",
        "  CHECK (tag_source IN (",
        sql_in_list(TAG_SOURCES),
        "  ));",
        "",
        "ALTER TABLE harald_documents ADD CONSTRAINT harald_doc_trust_ck",
        "  CHECK (trust_level IN (",
        sql_in_list(TRUST_LEVELS),
        "  ));",
        END,
    ])


def apply(text: str) -> str:
    start, stop = text.find(BEGIN), text.find(END)
    if start == -1 or stop == -1:
        raise SystemExit(
            "markers not found in harald_schema.sql; add the BEGIN/END pair first")
    return text[:start] + block() + text[stop + len(END):]


if __name__ == "__main__":
    original = SCHEMA.read_text(encoding="utf-8")
    updated = apply(original)
    if updated == original:
        print("schema already current")
    else:
        SCHEMA.write_text(updated, encoding="utf-8")
        print(f"regenerated vocabulary block in {SCHEMA.name}")
    print(f"  {len(SECTIONS)} sections, {len(MODULES)} modules, "
          f"{len(TAG_SOURCES)} tag sources, {len(TRUST_LEVELS)} trust levels")
