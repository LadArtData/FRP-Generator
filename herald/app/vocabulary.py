"""The tag vocabulary. One definition, imported by everything that reads or
writes a tag.

This module exists because of a specific, silent failure. The loader wrote
'transmittal', the classifier asked for 'cover_letter', and the query returned
zero rows with no error at all — no exception, no warning, just an empty
library. Three components each held their own copy of the vocabulary and none
of them agreed.

So the constants live here once. The classifier and the tagger import them, the
database CHECK constraints in schema/harald_schema.sql are generated from them
by tools/gen_vocabulary_sql.py, and a test fails the build if the SQL and this
file ever drift apart. A tag that is not in this file cannot be written to the
database, and a tag in this file that the SQL rejects cannot reach production.

On the merge that produced this list: the two generations disagreed on four
values. The FastAPI classifier emitted 'solution' and 'compliance', which the
HARALD CHECK constraint did not allow — every such chunk would have thrown
ORA-02290. HARALD carried 'risk' and 'general', which the classifier never
emitted, though 'general' arrives as the column default. The union is kept: a
tag that either generation considered real is real, because narrowing it is how
the empty-library failure happens again.
"""
from __future__ import annotations

# Where a passage sits in a proposal. Order is the order a proposal is read in,
# which is also the order the slot assembler walks.
SECTIONS: tuple[str, ...] = (
    "transmittal",      # cover letter, letter of transmittal
    "exec_summary",
    "qualifications",   # firm background, corporate experience
    "solution",         # proposed solution, why iteria
    "methodology",      # implementation approach, work plan, phases
    "project_mgmt",     # governance, timeline, milestones
    "staffing",         # key personnel, org chart, resumes
    "references",       # past performance, case studies
    "technical",        # architecture, integration, security, conversion
    "support",          # maintenance, training, SLA, help desk
    "cost",             # cost proposal, pricing, fee schedule
    "contract",         # terms and conditions, exceptions
    "compliance",       # mandatory requirements, requirement matrix
    "risk",             # risk register and mitigation
    "general",          # scored nothing; also the column default
)

# Which ERP module a passage is about.
MODULES: tuple[str, ...] = (
    "HCM",
    "PAYROLL",
    "FIN",
    "BUDGET",
    "PROC",
    "INV",
    "TECH",
    "CROSS",        # genuinely spans modules; not the same as GENERAL
    "GENERAL",      # scored nothing; also the column default
)

# How a tag was arrived at. Retrieval ranks measured tags above inherited ones,
# so this is not documentation: it changes results.
TAG_SOURCES: tuple[str, ...] = (
    "body",         # the chunk text itself scored above threshold
    "smoothed",     # sits between two body-scored anchors that agree
    "manual",       # a human overrode it
)

# Whether a document's factual claims may be reused, as opposed to its voice.
TRUST_LEVELS: tuple[str, ...] = (
    "VERIFIED",     # iteria wrote and submitted this; its facts can be reused
    "UNVERIFIED",   # iteria material whose specifics were never checked
)

DEFAULT_SECTION = "general"
DEFAULT_MODULE = "GENERAL"
DEFAULT_TAG_SOURCE = "body"
DEFAULT_TRUST = "VERIFIED"


def sql_in_list(values: tuple[str, ...], indent: int = 4, width: int = 74) -> str:
    """Render a vocabulary as the body of a SQL IN (...) list, wrapped to stay
    readable in Database Actions. Used to generate the CHECK constraints."""
    pad = " " * indent
    lines: list[str] = []
    current = pad
    for i, value in enumerate(values):
        piece = f"'{value}'" + ("," if i < len(values) - 1 else "")
        if len(current) + len(piece) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = pad
        current += piece + " "
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)
