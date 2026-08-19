"""Attestation checking.

A proposal may not claim something about its own attachments that the
attachments do not support. This module exists because three of four live
responses did exactly that, and every one of them would have been caught here:

  Nashua   "They have been completed by iteria functional leads accordingly."
           The Appendix A workbook held 45 of roughly 3,000 requirements.
  Jefferson "Requirements falling within the property tax collection areas are
           marked as not proposed rather than answered."
           The workbook answered 141 of them Standard or Configuration and
           filed no No Bid at all.
  Jefferson "We have priced the SaaS model on Attachment C1."
           Attachment C1 contained no figures.

None of those is a writing problem. Each is a claim of fact about a file, made
without reading the file, in a document the solicitation incorporates into the
resulting contract. A misstatement of that kind is disqualifying on its own
terms, and it is trivially checkable against data the system already holds.

The check is deliberately blunt. It does not attempt to understand the
narrative. It finds sentences that assert completeness, works out which
attachment each one is talking about, and compares the assertion against the
recorded state of that attachment. False positives are cheap -- an operator
dismisses a flag. A false negative is a disqualified bid.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("harald.attestations")

# Sentences that assert an attachment is done. Kept narrow and literal: these
# are the shapes that actually appeared, not a general theory of English.
_COMPLETION_CLAIM = re.compile(
    r"\b("
    r"(?:have|has|been|are|is|was|were)\s+(?:been\s+)?"
    r"(?:completed|filled|answered|populated|priced|prepared)"
    r"|complete(?:d)?\s+(?:by|in\s+full)"
    r"|fully\s+(?:completed|answered|priced)"
    r"|we\s+have\s+(?:completed|priced|answered|filled)"
    r"|responses?\s+(?:are|have\s+been)\s+(?:complete|completed|provided)"
    r"|submitted\s+(?:herewith|with\s+this\s+proposal|electronically)"
    r"|(?:is|are)\s+(?:included|attached|enclosed|provided)"
    r")\b",
    re.I,
)

# Sentences that assert something was deliberately NOT answered. Jefferson's
# failure was of this kind: the narrative claimed abstention, the workbook
# answered anyway.
_ABSTENTION_CLAIM = re.compile(
    r"\b("
    r"marked\s+as\s+not\s+proposed"
    r"|not\s+proposed\s+rather\s+than\s+answered"
    r"|no\s+bid(?:ded)?"
    r"|(?:are|is)\s+not\s+answered"
    r"|left\s+(?:blank|unanswered)"
    r"|declin(?:e|ed|ing)\s+to\s+(?:bid|respond|answer)"
    r"|outside\s+our\s+proposed\s+scope"
    r")\b",
    re.I,
)

# How a sentence names the thing it is claiming about.
_ATTACHMENT_REF = re.compile(
    r"\b((?:attachment|appendix|exhibit|schedule|worksheet|workbook|matrix|form)"
    r"(?:\s+[A-Z]\d?\b|\s+\d+\b)?)",
    re.I,
)

# Codes that mean "we are not offering this". Anything else is an answer.
_ABSTAIN_CODES = {"no bid", "nobid", "no-bid", "not proposed", "n/a", "na",
                  "not applicable", "not offered", "declined"}

# Below this share of rows carrying a response, a workbook is not "completed"
# by any reading an evaluator would accept.
COMPLETE_THRESHOLD = 0.95

# A multi-tab requirements workbook that yields fewer rows than this per sheet
# was almost certainly mis-parsed rather than genuinely short.
MIN_ITEMS_PER_SHEET = 8
MIN_SHEETS_FOR_DENSITY = 4


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def summarise(questionnaire: dict) -> dict:
    """Reduce a questionnaire to the few numbers an attestation can be checked
    against."""
    items = questionnaire.get("items") or []
    total = len(items)
    answered = 0
    abstained = 0
    placeable = 0
    layouts = {
        entry.get("sheet"): entry
        for entry in (questionnaire.get("sheet_map") or [])
        if isinstance(entry, dict)
    }
    for item in items:
        code = (item.get("response_code") or "").strip()
        if code:
            answered += 1
            if _norm(code) in _ABSTAIN_CODES:
                abstained += 1
        sheet = layouts.get(item.get("sheet")) or {}
        if sheet.get("code_columns") or item.get("response_col") or item.get("comment_col"):
            placeable += 1

    sheets_detected = sum(1 for entry in layouts.values() if entry.get("detected"))

    return {
        "q_id": questionnaire.get("q_id"),
        "filename": questionnaire.get("filename") or "",
        "total": total,
        "answered": answered,
        "abstained": abstained,
        "placeable": placeable,
        "sheets_detected": sheets_detected,
        "per_sheet": (total / sheets_detected) if sheets_detected else float(total),
        "answered_ratio": (answered / total) if total else 0.0,
        "placeable_ratio": (placeable / total) if total else 0.0,
    }


def _mentions(sentence: str, summary: dict) -> bool:
    """Does this sentence appear to be about this attachment?"""
    stem = _norm(summary["filename"].rsplit(".", 1)[0])
    if not stem:
        return False
    sentence_norm = _norm(sentence)
    if stem and stem in sentence_norm:
        return True
    # "Attachment C1", "Appendix A" -- match the label out of the filename.
    for label in _ATTACHMENT_REF.findall(summary["filename"]):
        if _norm(label) and _norm(label) in sentence_norm:
            return True
    return False


# A claim rarely names its attachment in the same sentence. Nashua's reads
# "They have been completed by iteria functional leads accordingly." -- the
# subject is two sentences back. Jefferson names Attachment B in the sentence
# after the claim. So the reference is looked for in a window around the claim,
# the way a reader would resolve it.
CONTEXT_SENTENCES = 3

# ...and no further than this many lines away, so the window cannot reach past
# a heading into a section about a different attachment.
CONTEXT_LINES = 2

# Generic ways a draft refers to its own attachments without naming one.
_GENERIC_REF = re.compile(
    r"\b(worksheets?|workbooks?|requirements? matri(?:x|ces)|matri(?:x|ces)"
    r"|attachments?|appendi(?:x|ces)|exhibits?|these forms?|the forms?)\b", re.I)


def _split_sentences(text: str) -> list[tuple[int, str]]:
    sentences: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if len(part.split()) >= 4:
                sentences.append((line_no, part))
    return sentences


def check(draft: str, questionnaires: list[dict]) -> list[dict]:
    """Return every attestation in ``draft`` the attachments do not support.

    Each finding carries the sentence, the file it refers to, and the numbers
    that contradict it, so an operator can act without re-deriving anything.
    """
    summaries = [summarise(q) for q in questionnaires or []]
    findings: list[dict] = []
    sentences = _split_sentences(draft or "")

    for index, (line_no, sentence) in enumerate(sentences):
        claims_done = bool(_COMPLETION_CLAIM.search(sentence))
        claims_abstained = bool(_ABSTENTION_CLAIM.search(sentence))
        if not (claims_done or claims_abstained):
            continue

        # Context is bounded by distance on the page as well as by sentence
        # count. Without the line bound the window reaches across a heading into
        # the next tab and binds a claim to an attachment discussed in a
        # different section entirely.
        window = " ".join(
            text for other_line, text in sentences[max(0, index - CONTEXT_SENTENCES):
                                                   index + CONTEXT_SENTENCES + 1]
            if abs(other_line - line_no) <= CONTEXT_LINES
        )

        for summary in summaries:
            named = _mentions(window, summary)
            # An unnamed reference is only safe to resolve when there is one
            # attachment it could possibly mean.
            generic = (len(summaries) == 1 and bool(_GENERIC_REF.search(window)))
            if not (named or generic):
                continue

            # A workbook can be 100% answered and still be wrong, if the import
            # only ever saw a fraction of it. Nashua's Appendix A reported
            # 45 of 45 answered across 23 functional tabs -- under two
            # requirements per module -- and every ratio below reads as perfect.
            # Sheet count is the one number the extraction bug could not fake.
            if (claims_done and summary["sheets_detected"] >= MIN_SHEETS_FOR_DENSITY
                    and summary["per_sheet"] < MIN_ITEMS_PER_SHEET):
                findings.append(_finding(
                    line_no, sentence, summary,
                    "claims_complete_but_thin",
                    f"The proposal states this attachment is complete. Only "
                    f"{summary['total']} rows were imported from "
                    f"{summary['sheets_detected']} detected sheets, about "
                    f"{summary['per_sheet']:.1f} per sheet. A requirements "
                    f"workbook that sparse usually means the import missed the "
                    f"body of the sheet rather than that the agency asked "
                    f"little."))
                continue

            if claims_done and summary["total"] == 0:
                findings.append(_finding(
                    line_no, sentence, summary,
                    "claims_complete_but_empty",
                    "The proposal states this attachment is complete. No rows "
                    "were imported from it at all."))
                continue

            if claims_done and summary["placeable_ratio"] < COMPLETE_THRESHOLD:
                findings.append(_finding(
                    line_no, sentence, summary,
                    "claims_complete_but_unwritable",
                    f"The proposal states this attachment is complete. Only "
                    f"{summary['placeable']} of {summary['total']} rows have "
                    f"anywhere to write an answer, so the exported file cannot "
                    f"carry the rest."))
                continue

            if claims_done and summary["answered_ratio"] < COMPLETE_THRESHOLD:
                findings.append(_finding(
                    line_no, sentence, summary,
                    "claims_complete_but_unanswered",
                    f"The proposal states this attachment is complete. "
                    f"{summary['answered']} of {summary['total']} rows carry a "
                    f"response."))
                continue

            if claims_abstained and summary["abstained"] == 0 and summary["answered"]:
                findings.append(_finding(
                    line_no, sentence, summary,
                    "claims_abstention_but_answered",
                    f"The proposal states these requirements were not answered. "
                    f"The workbook answers {summary['answered']} of "
                    f"{summary['total']} and records no abstention on any row."))

    if findings:
        log.warning("attestation check found %s contradiction(s)", len(findings))
    return findings


def _finding(line_no: int, sentence: str, summary: dict, kind: str,
             detail: str) -> dict:
    return {
        "line": line_no,
        "kind": kind,
        "sentence": sentence.strip(),
        "filename": summary["filename"],
        "q_id": summary["q_id"],
        "detail": detail,
        "total": summary["total"],
        "answered": summary["answered"],
        "placeable": summary["placeable"],
        "abstained": summary["abstained"],
    }
