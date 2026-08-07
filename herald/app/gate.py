"""
HARALD gate.

Runs the voice check over a finished document. No generation, no model,
no database. Point it at a .docx and it tells you what an evaluator is
going to notice before you send it.

This covers proposals a consultant wrote by hand, which is most of them.
It is also the only piece of HARALD that produces a number that moves
month to month, which matters when the people who need convincing are
leadership rather than the pipeline.

Scores against the style anchor, so the target is iteria's own best
document rather than a general notion of good writing.

    python harald_gate.py proposal.docx
    python harald_gate.py proposal.docx --section-detail
    python harald_gate.py *.docx --json > gate.json
"""

from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field, asdict

from . import chunking, voice


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------
def read_bytes(filename: str, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """Returns (full_text, [(heading, body), ...]).

    Headings come from Word styles where present, heuristics otherwise. Takes
    bytes rather than a path because the API scores uploads, and the CLI below
    reads the file itself.
    """
    name = (filename or "").lower()
    if name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
        blocks = [("H" if chunking._is_heading(line) else "P", line.strip())
                  for line in text.splitlines() if line.strip()]
    else:
        blocks = chunking.extract(filename, data)

    blocks = [(kind, text) for kind, text in blocks
              if not chunking.is_furniture(text)]
    sections = chunking._sections(blocks)
    return (" ".join(body for _, body in sections),
            [(heading, body) for heading, body in sections if body.strip()])


def read_document(path: str) -> tuple[str, list[tuple[str, str]]]:
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".docx", ".txt", ".md"):
        raise ValueError(f"unsupported file type {ext!r}; .docx, .txt or .md")
    with open(path, "rb") as fh:
        return read_bytes(os.path.basename(path), fh.read())


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------
@dataclass
class SectionScore:
    heading: str
    words: int
    banned_per_10k: float
    dashes: int
    rhythm: dict
    rhythm_flat: bool
    banned_words: list = field(default_factory=list)


@dataclass
class GateReport:
    path: str
    words: int
    sentences: int
    banned_total: int
    banned_per_10k: float
    banned_words: list
    dashes: int
    signposts: list
    hedge_stacks: list
    rhythm: dict
    rhythm_flat: bool
    anchor: dict
    verdict: str
    rhythm_sample_too_small: bool = False
    sections: list = field(default_factory=list)

    def as_dict(self):
        d = asdict(self)
        d["sections"] = [asdict(s) for s in self.sections]
        return d


# The anchor measures 5.2 banned words per 10k. A generated or edited
# document should be at or under it; the corpus average is 30.1.
BANNED_TARGET_PER_10K = 5.2
BANNED_FAIL_PER_10K = 15.0

# Rhythm is a distribution, and a distribution needs a sample. Attachment C2
# failed on rhythm with zero flagged words: 1,600 words of form fields is not
# prose, and judging its sentence lengths against a 16,000-word narrative says
# nothing. Below this count the rhythm figures are still reported, but they no
# longer decide the verdict.
MIN_RHYTHM_SENTENCES = 40


def _rate(n, words):
    return round(n * 10000 / words, 1) if words else 0.0


def score_document(path: str, section_detail: bool = False) -> GateReport:
    text, sections = read_document(path)
    words = len(text.split())
    if words < 100:
        raise ValueError(f"{path}: only {words} words of body text found")

    f = voice.score(text)
    banned_total = f["banned_word_count"]
    rate = _rate(banned_total, words)
    dashes = sum(f["forbidden_chars"].values()) if f["forbidden_chars"] else 0

    sentences = f["rhythm"].get("sentences", 0)
    rhythm_counts = sentences >= MIN_RHYTHM_SENTENCES
    rhythm_flat = bool(f["rhythm_flat"]) and rhythm_counts

    if rate <= BANNED_TARGET_PER_10K and not dashes and not rhythm_flat \
            and not f["signposts"]:
        verdict = "PASS"
    elif rate >= BANNED_FAIL_PER_10K or rhythm_flat:
        verdict = "FAIL"
    else:
        verdict = "REVIEW"

    sec_scores = []
    if section_detail:
        for heading, body in sections:
            w = len(body.split())
            if w < 60:
                continue
            sf = voice.score(body)
            sec_scores.append(SectionScore(
                heading=heading or "(no heading)",
                words=w,
                banned_per_10k=_rate(sf["banned_word_count"], w),
                dashes=sum(sf["forbidden_chars"].values()) if sf["forbidden_chars"] else 0,
                rhythm=sf["rhythm"],
                rhythm_flat=sf["rhythm_flat"],
                banned_words=sf["banned_words"],
            ))
        sec_scores.sort(key=lambda s: -s.banned_per_10k)

    return GateReport(
        path=path,
        words=words,
        sentences=sentences,
        banned_total=banned_total,
        banned_per_10k=rate,
        banned_words=f["banned_words"],
        dashes=dashes,
        signposts=f["signposts"],
        hedge_stacks=f["hedge_stacks"],
        rhythm=f["rhythm"],
        rhythm_flat=rhythm_flat,
        rhythm_sample_too_small=not rhythm_counts,
        anchor={
            "banned_per_10k": BANNED_TARGET_PER_10K,
            "mean_words": voice.RHYTHM["mean_words"],
            "share_under_10_min": voice.RHYTHM["short_under_10_min"],
            "share_15_25_max": voice.RHYTHM["band_15_25_max"],
            "source": voice.CALIBRATION["source"],
        },
        verdict=verdict,
        sections=sec_scores,
    )


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def render(report: GateReport, section_detail: bool = False) -> str:
    r = report
    a = r.anchor
    out = [
        f"{os.path.basename(r.path)}",
        f"  {r.words:,} words, {r.sentences:,} sentences",
        "",
        f"  VERDICT: {r.verdict}",
        "",
        f"  flagged vocabulary   {r.banned_total:>4} total, {r.banned_per_10k:>5} per 10k"
        f"   (anchor {a['banned_per_10k']}, library average 30.1)",
        f"  em and en dashes     {r.dashes:>4}"
        f"                        (target 0)",
    ]
    rh = r.rhythm
    if rh:
        out += [
            f"  mean sentence        {rh.get('mean'):>4} words"
            f"                  (anchor {a['mean_words']})",
            f"  under 10 words       {rh.get('share_under_10', 0):>4.0%}"
            f"                        (floor {a['share_under_10_min']:.0%})",
            f"  in the 15-25 band    {rh.get('share_15_25', 0):>4.0%}"
            f"                        (ceiling {a['share_15_25_max']:.0%})",
            f"  over 35 words        {rh.get('share_over_35', 0):>4.0%}",
        ]
    if r.rhythm_flat:
        out.append("  rhythm reads mechanical against the anchor")
    if r.rhythm_sample_too_small:
        out.append(f"  rhythm not judged: {r.sentences} sentences, "
                   f"under the {MIN_RHYTHM_SENTENCES} needed for the distribution "
                   f"to mean anything")
    if r.banned_words:
        out += ["", "  words to replace:"]
        for w in r.banned_words:
            out.append(f"    {w:<16} -> {voice.BANNED[w]}")
    if r.signposts:
        out += ["", "  signposting to delete: " + ", ".join(r.signposts)]
    if r.hedge_stacks:
        out.append("  stacked hedges present")

    if section_detail and r.sections:
        out += ["", "  BY SECTION, worst first:"]
        for s in r.sections:
            flag = " FLAT" if s.rhythm_flat else ""
            out.append(f"    {s.banned_per_10k:>6}/10k  {s.dashes:>3} dashes  "
                       f"{s.words:>5}w{flag}  {s.heading[:48]}")
    out.append("")
    out.append(f"  measured against: {a['source']}")
    return "\n".join(out)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Score a finished proposal against iteria's style anchor.")
    ap.add_argument("paths", nargs="+", help=".docx, .txt or .md files")
    ap.add_argument("--section-detail", action="store_true",
                    help="break the score down by document section")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    a = ap.parse_args(argv)

    expanded = []
    for p in a.paths:
        hits = glob.glob(p)
        expanded.extend(hits if hits else [p])

    reports, failures = [], []
    for path in expanded:
        try:
            reports.append(score_document(path, section_detail=a.section_detail))
        except Exception as exc:
            failures.append((path, str(exc)))

    if a.json:
        print(json.dumps({
            "reports": [r.as_dict() for r in reports],
            "errors": [{"path": p, "error": e} for p, e in failures],
        }, indent=2))
    else:
        for r in reports:
            print(render(r, section_detail=a.section_detail))
            print("-" * 72)
        for p, e in failures:
            print(f"could not read {p}: {e}", file=sys.stderr)
        if len(reports) > 1:
            worst = max(reports, key=lambda r: r.banned_per_10k)
            avg = statistics.mean(r.banned_per_10k for r in reports)
            print(f"{len(reports)} documents, mean {avg:.1f} flagged words per 10k, "
                  f"worst {os.path.basename(worst.path)} at {worst.banned_per_10k}")

    return 1 if any(r.verdict == "FAIL" for r in reports) or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
