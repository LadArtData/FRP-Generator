"""Scan for Oracle reserved bind variable names that cause ORA-01745.

Only inspects SQL-looking string literals (contain INSERT/UPDATE/SELECT/etc.),
so Python slice notation and regexes are ignored.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ORACLE_SQL_RESERVED = {
    "access", "add", "all", "alter", "and", "any", "as", "asc", "audit",
    "between", "by", "char", "check", "cluster", "column", "comment",
    "compress", "connect", "create", "current", "date", "decimal", "default",
    "delete", "desc", "distinct", "drop", "else", "exclusive", "exists",
    "file", "float", "for", "from", "grant", "group", "having", "identified",
    "immediate", "in", "increment", "index", "initial", "insert", "integer",
    "intersect", "into", "is", "level", "like", "lock", "long", "maxextents",
    "minus", "mode", "modify", "noaudit", "nocompress", "not", "nowait",
    "null", "number", "of", "offline", "on", "online", "option", "or", "order",
    "pctfree", "prior", "privileges", "public", "raw", "rename", "resource",
    "revoke", "row", "rowid", "rownum", "rows", "select", "session", "set",
    "share", "size", "smallint", "start", "successful", "synonym", "sysdate",
    "table", "then", "to", "trigger", "uid", "union", "unique", "update",
    "user", "validate", "values", "varchar", "varchar2", "view", "whenever",
    "where", "with",
    # Not always in the formal list, but known to trip ORA-01745 in practice.
    "state", "type",
}

BIND_PAT = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
# Triple-quoted or single/double quoted strings that look like SQL.
SQL_STRING_PAT = re.compile(
    r"""(?xs)
    (?:'''|\"\"\")(.*?)(?:'''|\"\"\")
    |
    '(?:[^'\\]|\\.)*'
    |
    "(?:[^"\\]|\\.)*"
    """
)
SQL_HINT = re.compile(
    r"\b(INSERT|UPDATE|DELETE|SELECT|MERGE|RETURNING|INTO|FROM|WHERE|VALUES)\b",
    re.I,
)
ROOTS = [Path("app"), Path("tools")]


def main() -> int:
    hits: list[tuple[str, int, str, str]] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path.name == "scan_bind_names.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for sm in SQL_STRING_PAT.finditer(text):
                body = sm.group(1) if sm.group(1) is not None else sm.group(0)
                if not SQL_HINT.search(body):
                    continue
                # Line number of the start of this string.
                line_no = text.count("\n", 0, sm.start()) + 1
                for match in BIND_PAT.finditer(body):
                    name = match.group(1).lower()
                    if name in ORACLE_SQL_RESERVED:
                        snippet = body.strip().replace("\n", " ")[:140]
                        hits.append((str(path), line_no, name, snippet))

    seen: set[tuple[str, str]] = set()
    uniq = []
    for item in hits:
        key = (item[0], item[2])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)

    print(f"Found {len(hits)} reserved-bind hits ({len(uniq)} unique file+name)\n")
    for path, line_no, name, snippet in sorted(uniq):
        print(f"{path}:{line_no}  :{name}")
        print(f"  {snippet}")
    return 1 if uniq else 0


if __name__ == "__main__":
    sys.exit(main())
