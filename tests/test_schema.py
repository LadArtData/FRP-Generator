"""Static checks on harald_schema.sql.

None of this replaces running the script, but each case here is a mistake that
has actually cost a round trip through Database Actions: a substitution prompt
on an ampersand, an unterminated PL/SQL block, tables landing in ADMIN instead
of ITERIA_AI. They are cheap to check and expensive to discover.
"""
import os
import pathlib
import re

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "schema" / "harald_schema.sql"
SQL = SCHEMA.read_text(encoding="utf-8")


class TestRunsUnderTheAdminLogin:
    def test_define_is_off(self):
        """Without this an '&' in any literal opens a 'Enter value for...' prompt
        and the script stalls waiting on input that never comes."""
        assert re.search(r"^SET DEFINE OFF\b", SQL, re.M | re.I)

    def test_current_schema_is_set(self):
        """ADMIN is the only login, but every table belongs to ITERIA_AI. Without
        this the whole script succeeds and builds the application in the wrong
        schema, which looks like it worked."""
        assert re.search(r"ALTER SESSION SET CURRENT_SCHEMA\s*=\s*iteria_ai", SQL, re.I)

    def test_no_bare_ampersands_outside_comments(self):
        for number, line in enumerate(SQL.splitlines(), 1):
            if line.lstrip().startswith("--"):
                continue
            assert "&" not in line, f"line {number} has a bare & that will prompt: {line.strip()}"


class TestPlsqlBlocksAreTerminated:
    def test_every_trigger_block_has_a_slash(self):
        """A PL/SQL block without a following '/' is silently buffered and never
        compiled, so the trigger simply does not exist."""
        blocks = re.findall(
            r"CREATE OR REPLACE TRIGGER\s+(\w+).*?(?=(?:^/\s*$)|\Z)",
            SQL, re.S | re.M | re.I)
        slashes = len(re.findall(r"^/\s*$", SQL, re.M))
        assert blocks, "expected the retention triggers to be present"
        assert slashes >= len(blocks), (
            f"{len(blocks)} PL/SQL blocks but only {slashes} '/' terminators")

    def test_quotes_balance_across_the_file(self):
        """Scanned rather than counted. A literal can contain a semicolon and a
        comment marker, so splitting on either before knowing what is inside a
        string gets the answer wrong."""
        in_string = False
        line = 1
        opened_at = 0
        index = 0
        while index < len(SQL):
            char = SQL[index]
            if char == "\n":
                line += 1
            if in_string:
                if char == "'":
                    if SQL[index + 1:index + 2] == "'":   # '' is an escaped quote
                        index += 2
                        continue
                    in_string = False
            else:
                if char == "-" and SQL[index + 1:index + 2] == "-":
                    newline = SQL.find("\n", index)
                    index = len(SQL) if newline == -1 else newline
                    continue
                if char == "'":
                    in_string, opened_at = True, line
            index += 1
        assert not in_string, f"unterminated string literal opened on line {opened_at}"


class TestRetention:
    def test_approved_and_submitted_packages_are_protected(self):
        assert "harald_pkg_retain_trg" in SQL
        trigger = SQL[SQL.index("harald_pkg_retain_trg"):]
        assert "'approved'" in trigger[:400] and "'submitted'" in trigger[:400]

    def test_library_documents_are_protected(self):
        assert "harald_doc_retain_trg" in SQL

    def test_error_numbers_are_in_the_user_defined_range(self):
        for code in re.findall(r"RAISE_APPLICATION_ERROR\(\s*(-\d+)", SQL):
            assert -20999 <= int(code) <= -20000, f"{code} is outside the allowed range"

    def test_the_message_says_how_to_proceed_deliberately(self):
        """A block with no way past it gets worked around with a bigger hammer."""
        for match in re.finditer(r"RAISE_APPLICATION_ERROR\((.*?)\);", SQL, re.S):
            assert "UPDATE" in match.group(1), \
                "retention message must name the deliberate path, not just refuse"
