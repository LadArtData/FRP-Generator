"""Body-text tagging for retrieval chunks.

Tags are derived from the chunk body, never from the filename and never carried
forward from the previous chunk. That is what produced the defect this module
replaces: 77 chunks stamped 'cost' when only 17 of them said anything about
money, because a table-of-contents line ("18 Cost Proposal") bled its label down
the whole document. Measured against the nine-proposal library, body scoring
took cost precision from 22 percent to effectively all of it, and grew the pool
of chunks usable for HCM retrieval from 29 to 95.

Two mechanisms keep it honest. Each pattern can contribute at most _HIT_CAP
hits, so one repeated word cannot outvote genuine topical spread. And a tag
below its minimum score is not a weak guess, it is 'general' — the caller can
then decide whether to smooth it from neighbours, and the result records which
of the two happened in tag_source.

The vocabulary is imported, not restated. Two values, 'solution' and
'compliance', carry no scoring patterns here: they are reached through heading
detection in classifier.section_of, which is where the previous generation
recognised them. They are deliberately left unscored rather than given invented
patterns, because the precision figures above were measured and a fabricated
pattern would quietly degrade them.
"""
from __future__ import annotations

import re

from .vocabulary import DEFAULT_MODULE, DEFAULT_SECTION, MODULES, SECTIONS

# (pattern, weight). Weight reflects how decisive the phrase is.
_SECTION_PATS: list[tuple[str, list[tuple[str, int]]]] = [
    ("transmittal", [
        (r"dear (evaluation committee|mr\.|ms\.|members)", 5),
        (r"letter of transmittal|transmittal letter", 5),
        (r"respectfully submitted", 4),
        (r"is pleased to submit", 3),
        (r"we welcome the opportunity", 2),
    ]),
    ("exec_summary", [
        (r"executive (summary|narrative)|exective summary", 5),
        (r"why (iteria|we are) the right (partner|fit)", 3),
        (r"at a glance|in summary,", 2),
    ]),
    ("methodology", [
        (r"implementation (approach|methodology|plan)", 5),
        (r"project (approach|methodology)", 5),
        (r"\b(discovery|design|configure|test|deploy)\b.{0,40}\bphase\b", 3),
        (r"phase [1-9one-five]+\b", 3),
        (r"work ?plan|methodolog|cutover|go-?live plan", 3),
        (r"conference room pilot|\bcrp\b|sprint", 2),
    ]),
    ("project_mgmt", [
        (r"project (management|governance|schedule|charter)", 5),
        (r"status report|steering committee|change (control|management) (plan|process)", 4),
        (r"milestone|work breakdown|\bwbs\b|gantt", 3),
        (r"escalation (path|procedure)", 3),
    ]),
    ("staffing", [
        (r"key personnel|project team|team structure|org(anization)? chart", 5),
        (r"resume|curriculum vitae", 4),
        (r"\b(project manager|functional lead|technical lead|solution architect)\b", 3),
        (r"staffing plan|level of effort|hours? (allocated|by resource)", 3),
        (r"years of experience", 2),
    ]),
    ("qualifications", [
        (r"company (background|profile|history|overview)", 5),
        (r"firm qualifications|corporate experience|about (iteria|us)", 5),
        (r"oracle (gold |platinum )?partner", 3),
        (r"\bs corporation\b|incorporated in|headquartered", 3),
        (r"financial (statement|stability)|dun & bradstreet", 3),
    ]),
    ("references", [
        (r"\breferences?\b.{0,30}\b(client|form|contact)", 5),
        (r"past performance|reference (client|check|form)", 5),
        (r"case stud|client success", 3),
        (r"contact (name|person).{0,40}(phone|email)", 2),
    ]),
    ("technical", [
        (r"technical (architecture|requirement|approach|specification)", 5),
        (r"system architecture|infrastructure|hosting|data center", 4),
        (r"integration (approach|architecture|strategy)|\boic\b", 4),
        (r"data (migration|conversion|cleansing)", 4),
        (r"single sign-?on|\bsso\b|encryption|security (model|architecture)", 3),
        (r"\bapi\b|web service|middleware", 2),
    ]),
    ("support", [
        (r"(post|after)[- ]?(implementation|go-?live) support", 5),
        (r"help ?desk|service desk|ticket", 4),
        (r"maintenance and support|managed services|\bsla\b|service level", 4),
        (r"(end user |user )?training (plan|approach|program|materials)", 4),
        (r"knowledge transfer|hypercare", 4),
        (r"quarterly (update|release)|patch(ing)? cycle", 3),
    ]),
    ("contract", [
        (r"terms and conditions|contract terms|general provisions", 5),
        (r"indemnif|liabilit|warrant(y|ies)|termination for", 4),
        (r"insurance (requirement|certificate)|workers.{0,3} compensation", 4),
        (r"\bexception(s)? (to|taken)\b|we take no exception", 4),
        (r"confidential(ity)?|proprietary|public records", 3),
        (r"non-?collusion|e-?verify|\bw-9\b|addend(um|a)", 3),
        (r"signature (page|of)|authorized representative", 2),
    ]),
    ("cost", [
        (r"cost proposal|price proposal|fee schedule|pricing (table|summary|sheet)", 5),
        (r"\$[\d,]{3,}", 4),
        (r"hourly rate|blended rate|rate card|not[- ]to[- ]exceed", 4),
        (r"total (cost|price)|cost of ownership|\btco\b", 4),
        (r"invoice|payment (terms|schedule)|milestone payment", 3),
        (r"license (fee|cost)|subscription (fee|cost)", 3),
    ]),
    ("risk", [
        (r"risk (management|register|mitigation|assessment)", 5),
        (r"mitigation strateg|contingency plan", 4),
        (r"\brisks?\b.{0,40}\b(identif|mitigat|likelihood|impact)", 3),
        (r"lessons learned", 2),
    ]),
]

_MODULE_PATS: list[tuple[str, list[tuple[str, int]]]] = [
    ("HCM", [
        (r"human capital management|\bhcm\b", 5),
        (r"\bcore ?hr\b|human resources?\b", 4),
        (r"employee self[- ]?service|manager self[- ]?service|\bess\b|\bmss\b", 4),
        (r"position management|job (requisition|family)|absence management", 4),
        (r"benefits? (enrollment|administration|open enrollment)", 4),
        (r"talent (management|acquisition)|recruit|onboard|performance review", 3),
        (r"\bfmla\b|leave of absence|collective bargaining", 3),
    ]),
    ("PAYROLL", [
        (r"\bpayroll\b", 5),
        (r"time (and attendance|entry|card)|\btimekeeping\b|\bukg\b|kronos", 4),
        (r"earnings? (code|element)|deduction (code|element)|garnishment", 4),
        (r"\bw-2\b|941|tax filing|direct deposit|pay ?check|pay ?period", 3),
        (r"retro(active)? pay|gross[- ]to[- ]net", 3),
    ]),
    ("FIN", [
        (r"general ledger|\bgl\b", 5),
        (r"accounts? payable|\bap\b invoice|accounts? receivable", 4),
        (r"chart of accounts|journal entr|subledger|period close|month[- ]end close", 4),
        (r"fixed assets?|depreciation|cash management|bank reconcil", 4),
        (r"fund accounting|grant (accounting|management)|\bgasb\b|\bcafr\b|\bacfr\b", 4),
        (r"financial (statement|reporting)|trial balance", 3),
    ]),
    ("BUDGET", [
        (r"budget (development|preparation|planning|book|module)", 5),
        (r"budgetary control|encumbrance|appropriation", 4),
        (r"\bepm\b|enterprise performance management|planning and budgeting", 4),
        (r"position budgeting|salary (projection|forecast)", 4),
        (r"multi[- ]?year (forecast|plan)|capital improvement (plan|program)|\bcip\b", 3),
    ]),
    ("PROC", [
        (r"procure(ment)?\b|purchasing", 5),
        (r"purchase (order|requisition)|\bpo\b|requisition", 4),
        (r"supplier (portal|management|registration)|vendor (portal|management)", 4),
        (r"sourcing|solicitation management|bid (evaluation|tabulation)", 4),
        (r"contract lifecycle|\bp2p\b|three[- ]way match", 3),
    ]),
    ("INV", [
        (r"inventory (management|control|count)", 5),
        (r"warehouse|stockroom|storeroom|cycle count", 4),
        (r"work order|maintenance management|\beam\b|asset tracking", 4),
        (r"supply chain|\bscm\b|receiving|item master", 3),
    ]),
    ("TECH", [
        (r"\boic\b|oracle integration cloud|integration (approach|architecture)", 4),
        (r"data (migration|conversion)|\betl\b|\bhdl\b", 4),
        (r"single sign-?on|\bsso\b|\bsaml\b|role[- ]based (access|security)", 4),
        (r"\botbi\b|\bbip?\b reports?|analytics|reporting (tool|framework)", 3),
        (r"disaster recovery|uptime|availability|infrastructure|"
         r"environment (strategy|refresh)", 3),
        (r"\bapi\b|web service|rest\b|\bsftp\b", 3),
    ]),
]


def _compile(patterns):
    return [(tag, [(re.compile(p, re.I), w) for p, w in plist])
            for tag, plist in patterns]


_SECTION_RES = _compile(_SECTION_PATS)
_MODULE_RES = _compile(_MODULE_PATS)

# A pattern emitting a tag the database rejects is an ORA-02290 halfway through
# an ingest. Caught at import instead.
for _name, _pats, _allowed in (("section", _SECTION_PATS, SECTIONS),
                               ("module", _MODULE_PATS, MODULES)):
    _unknown = {tag for tag, _ in _pats} - set(_allowed)
    if _unknown:
        raise RuntimeError(f"{_name} patterns emit tags absent from vocabulary: "
                           f"{sorted(_unknown)}")

# One pattern contributes at most this many hits, so a repeated word cannot
# outvote genuine topical spread.
_HIT_CAP = 3
_MIN_SECTION_SCORE = 4
_MIN_MODULE_SCORE = 4
_CROSS_MIN = 3          # distinct non-TECH modules at threshold to call it CROSS


def _score(text: str, compiled) -> dict[str, int]:
    scores: dict[str, int] = {}
    for tag, plist in compiled:
        total = 0
        for pattern, weight in plist:
            hits = len(pattern.findall(text))
            if hits:
                total += weight * min(hits, _HIT_CAP)
        if total:
            scores[tag] = total
    return scores


def tag_section(text: str) -> tuple[str, int]:
    """Best-supported section for a passage, with the score that earned it.
    Below threshold returns the default rather than a weak guess, so the caller
    can tell a measurement from an absence."""
    scores = _score(text, _SECTION_RES)
    if not scores:
        return DEFAULT_SECTION, 0
    tag, score = max(scores.items(), key=lambda kv: (kv[1], -SECTIONS.index(kv[0])))
    if score < _MIN_SECTION_SCORE:
        return DEFAULT_SECTION, score
    return tag, score


def tag_module(text: str) -> tuple[str, int]:
    """Best-supported module. Three or more non-TECH modules at threshold means
    the passage genuinely spans them, which is CROSS rather than a coin toss
    between them. TECH is excluded from that count because technical language
    appears throughout a proposal without the passage being about technology."""
    scores = _score(text, _MODULE_RES)
    strong = {k: v for k, v in scores.items() if v >= _MIN_MODULE_SCORE}
    if not strong:
        return DEFAULT_MODULE, 0
    if len([k for k in strong if k != "TECH"]) >= _CROSS_MIN:
        return "CROSS", sum(strong.values())
    tag, score = max(strong.items(), key=lambda kv: (kv[1], kv[0] != "TECH"))
    return tag, score


def normalize_dashes(text: str) -> str:
    """Em and en dashes out of the corpus. These chunks exist to be fed to a
    model as voice reference, and the original document is still on file.
    Leaving them in trains the humanize pass to emit the one character it is
    explicitly forbidden to emit."""
    out = text.replace("\u2014", ", ").replace("\u2013", " to ")
    out = out.replace(" , ", ", ").replace(", ,", ",")
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def smooth_sections(tagged: list[tuple[str, int]]) -> list[str]:
    """Fill unscored gaps between confident anchors, in chunk order.

    A proposal is a sequence of contiguous sections, so an unscored chunk
    between two chunks that both scored 'contract' is contract. This is bounded
    propagation between anchors, which is not what the old loader did: it took
    one label off a table-of-contents line and rode it down the whole document.

      1. Gap between two anchors with the same tag: fill with that tag.
      2. Gap between anchors that disagree: split at the midpoint, each half
         taking its nearer anchor.
      3. A run before the first anchor or after the last: extend that anchor
         outward at most MAX_EXTEND chunks, then stop.
    """
    MAX_EXTEND = 4
    out = [tag for tag, _ in tagged]
    anchors = [i for i, (tag, _) in enumerate(tagged) if tag != DEFAULT_SECTION]
    if not anchors:
        return out

    for a, b in zip(anchors, anchors[1:]):
        if b - a <= 1:
            continue
        if out[a] == out[b]:
            for i in range(a + 1, b):
                out[i] = out[a]
        else:
            mid = a + (b - a) // 2
            for i in range(a + 1, mid + 1):
                out[i] = out[a]
            for i in range(mid + 1, b):
                out[i] = out[b]

    first, last = anchors[0], anchors[-1]
    for i in range(max(0, first - MAX_EXTEND), first):
        out[i] = out[first]
    for i in range(last + 1, min(len(out), last + 1 + MAX_EXTEND)):
        out[i] = out[last]
    return out


def smooth_modules(tagged: list[tuple[str, int]], max_gap: int = 2) -> list[str]:
    """Lighter than section smoothing. Modules cluster but interleave (a payroll
    paragraph inside an HCM section is normal), so this only closes short gaps
    between anchors that already agree. No midpoint splitting, no outward
    extension, and GENERAL is a legitimate answer rather than a gap."""
    out = [tag for tag, _ in tagged]
    anchors = [i for i, (tag, _) in enumerate(tagged) if tag != DEFAULT_MODULE]
    for a, b in zip(anchors, anchors[1:]):
        if 1 < b - a <= max_gap + 1 and out[a] == out[b]:
            for i in range(a + 1, b):
                out[i] = out[a]
    return out
