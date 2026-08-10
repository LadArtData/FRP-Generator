"""The vocabulary must mean the same thing in Python and in the database.

The failure this guards against produced no error at all. The loader wrote
'transmittal', the classifier asked for 'cover_letter', and retrieval returned
an empty library — no exception, no warning, nothing in a log. Merging the two
generations nearly reintroduced it from the other direction: the FastAPI
classifier emits 'solution' and 'compliance', which HARALD's original CHECK
constraint did not allow, so every such chunk would have thrown ORA-02290 on
insert.

These tests fail the build rather than the ingest.
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

os.environ.setdefault("ORACLE_PASSWORD", "x")
os.environ.setdefault("ORACLE_DSN", "x")
os.environ.setdefault("GENAI_REGION", "us-chicago-1")
os.environ.setdefault("GENAI_MODEL_OCID", "ocid1.generativeaimodel.oc1.test.aaaa")
os.environ.setdefault("GENAI_COMPARTMENT_ID", "ocid1.compartment.oc1..test")
os.environ.setdefault("HARALD_SESSION_SECRET", "unit-test-secret-value")

import gen_vocabulary_sql as gen  # noqa: E402

from app import classifier, vocabulary  # noqa: E402

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema" / "harald_schema.sql"


class TestClassifierAgreesWithVocabulary:
    def test_sections_emitted_are_all_allowed(self):
        emitted = {tag for tag, _ in classifier._SECTION_PATTERNS}
        unknown = emitted - set(vocabulary.SECTIONS)
        assert not unknown, f"classifier emits sections the database rejects: {sorted(unknown)}"

    def test_modules_emitted_are_all_allowed(self):
        emitted = {tag for tag, _ in classifier._MODULE_PATTERNS}
        unknown = emitted - set(vocabulary.MODULES)
        assert not unknown, f"classifier emits modules the database rejects: {sorted(unknown)}"

    def test_column_defaults_are_in_range(self):
        # A default outside the CHECK makes every insert that omits the column fail.
        assert vocabulary.DEFAULT_SECTION in vocabulary.SECTIONS
        assert vocabulary.DEFAULT_MODULE in vocabulary.MODULES
        assert vocabulary.DEFAULT_TAG_SOURCE in vocabulary.TAG_SOURCES
        assert vocabulary.DEFAULT_TRUST in vocabulary.TRUST_LEVELS

    def test_no_duplicates(self):
        for name in ("SECTIONS", "MODULES", "TAG_SOURCES", "TRUST_LEVELS"):
            values = getattr(vocabulary, name)
            assert len(values) == len(set(values)), f"{name} contains duplicates"


class TestSchemaMatchesPython:
    def test_generated_block_is_current(self):
        assert gen.block() in SCHEMA.read_text(encoding="utf-8"), (
            "schema/harald_schema.sql is stale. Run: python tools/gen_vocabulary_sql.py")

    def test_every_value_reaches_the_sql(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        for value in (*vocabulary.SECTIONS, *vocabulary.MODULES,
                      *vocabulary.TAG_SOURCES, *vocabulary.TRUST_LEVELS):
            assert f"'{value}'" in schema, f"{value!r} never reaches the schema"

    def test_column_defaults_match_the_schema(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        for column, default in (
            ("section_tag", vocabulary.DEFAULT_SECTION),
            ("module_tag", vocabulary.DEFAULT_MODULE),
            ("tag_source", vocabulary.DEFAULT_TAG_SOURCE),
            ("trust_level", vocabulary.DEFAULT_TRUST),
        ):
            assert f"DEFAULT '{default}'" in schema, \
                f"{column} default {default!r} is not the one the schema declares"
