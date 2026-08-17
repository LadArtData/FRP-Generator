"""Run schema/harald_schema.sql against the database.

    python tools/deploy_schema.py            refuses if the library has content
    python tools/deploy_schema.py --force    drops and recreates regardless

This exists because the schema file is written for SQL*Plus and SQL Developer
Web, and neither is a comfortable place to be: the browser tool prompts for
substitution variables on any bare ampersand, and a PL/SQL block pasted into
the wrong pane fails in ways that read like a syntax error. Here the file is
parsed once and each statement is sent over the same connection the
application uses, so what deploys is what the application will find.

The file begins by dropping every HARALD table. That is correct for a build
that is being stood up and catastrophic for one that is in use, so this
counts rows first and stops if it finds any. Note that the retention triggers
in the schema guard DELETE and cannot guard DROP: no trigger fires on a drop,
so nothing in the database itself will stop that statement. This check is the
only thing standing in front of it.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA = ROOT / "schema" / "harald_schema.sql"

# SQL*Plus directives. The driver has no idea what these are, and they carry no
# meaning over a program connection: substitution is a SQL*Plus feature, so
# turning it off is a no-op here rather than something to emulate.
SQLPLUS = re.compile(r"^\s*(SET|SPOOL|PROMPT|WHENEVER|SHOW|COLUMN|CONNECT)\s", re.I)

# Only these are terminated by a lone / rather than by a semicolon, because
# they legitimately contain semicolons of their own.
PLSQL_START = re.compile(
    r"^\s*(BEGIN|DECLARE|CREATE\s+(OR\s+REPLACE\s+)?"
    r"(TRIGGER|PROCEDURE|FUNCTION|PACKAGE|TYPE)\b)", re.I)


def _has_code(lines: list[str]) -> bool:
    """Whether anything buffered is a statement rather than a comment."""
    return any(l.strip() and not l.strip().startswith("--") for l in lines)


def statements(sql: str) -> list[tuple[int, str]]:
    """Split the file into (line number, statement) pairs.

    The subtlety is that a statement is preceded by its comment block, and the
    comments must not be mistaken for its first line. Getting that wrong is
    quiet and expensive: a CREATE TRIGGER whose comment header is treated as
    the opening line is not recognised as PL/SQL, so it terminates at the first
    semicolon inside its own body and deploys as a truncated fragment.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    plsql = False

    for number, line in enumerate(sql.splitlines(), 1):
        stripped = line.strip()

        if not _has_code(buf):
            # Still ahead of the statement proper: directives are dropped,
            # blank lines discarded, comments carried along.
            if SQLPLUS.match(line):
                continue
            if not stripped:
                continue
            if stripped.startswith("--"):
                buf.append(line)
                continue
            start = number
            plsql = bool(PLSQL_START.match(line))

        if plsql and stripped == "/":
            out.append((start, "\n".join(buf)))
            buf = []
            continue

        buf.append(line)

        if not plsql and line.rstrip().endswith(";"):
            out.append((start, "\n".join(buf).rstrip().rstrip(";")))
            buf = []

    if _has_code(buf):
        out.append((start, "\n".join(buf)))
    return out


def label(statement: str) -> str:
    """A short name for progress output."""
    text = "\n".join(l for l in statement.splitlines()
                     if l.strip() and not l.strip().startswith("--"))
    text = " ".join(text.split())
    match = re.match(
        r"(CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?\w+|ALTER\s+\w+|"
        r"INSERT\s+INTO|COMMENT\s+ON\s+\w+|BEGIN|DECLARE)\s+(\S+)?", text, re.I)
    if not match:
        return text[:58]
    return " ".join(p for p in match.groups() if p)[:58].upper()


def occupied(cur, schema: str) -> list[tuple[str, int]]:
    """HARALD tables that already hold rows."""
    cur.execute("""SELECT table_name FROM all_tables
                    WHERE owner = :o AND table_name LIKE 'HARALD%'
                    ORDER BY table_name""", {"o": schema.upper()})
    found = []
    for (name,) in cur.fetchall():
        cur.execute(f'SELECT COUNT(*) FROM {schema}."{name}"')
        count = cur.fetchone()[0]
        if count:
            found.append((name, count))
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="drop and recreate even if tables hold rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and list the statements without running them")
    args = ap.parse_args()

    sql = SCHEMA.read_text(encoding="utf-8", errors="replace")
    parsed = statements(sql)
    print(f"{SCHEMA.name}: {len(parsed)} statements")

    if args.dry_run:
        for number, statement in parsed:
            print(f"  line {number:>4}  {label(statement)}")
        return 0

    from app import db
    from app.config import cfg

    pool = db.init_pool()
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {cfg.app_schema}")

        rows = occupied(cur, cfg.app_schema)
        if rows and not args.force:
            print("\nRefusing to run. These tables hold data and the first thing\n"
                  "this script does is drop them:")
            for name, count in rows:
                print(f"  {name:<32} {count:>8} rows")
            print("\nNo trigger can stop a DROP, so this check is the only guard.\n"
                  "Re-run with --force if replacing this really is the intent.")
            return 1
        if rows:
            print(f"\n--force: dropping {len(rows)} populated tables.")

        failures = 0
        for number, statement in parsed:
            try:
                cur.execute(statement)
                print(f"  ok    {label(statement)}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL  {label(statement)}  (line {number})")
                print(f"        {str(exc).strip().splitlines()[0]}")
        conn.commit()

        print()
        if failures:
            print(f"{failures} of {len(parsed)} statements failed.")
            return 1
        print(f"All {len(parsed)} statements ran. Verifying...")
        cur.execute("""SELECT COUNT(*) FROM all_tables
                        WHERE owner = :o AND table_name LIKE 'HARALD%'""",
                    {"o": cfg.app_schema.upper()})
        print(f"  {cur.fetchone()[0]} HARALD tables now in {cfg.app_schema}")
        cur.execute("""SELECT trigger_name, status FROM all_triggers
                        WHERE owner = :o AND trigger_name LIKE 'HARALD%RETAIN%'""",
                    {"o": cfg.app_schema.upper()})
        for name, status in cur.fetchall():
            print(f"  {name} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
