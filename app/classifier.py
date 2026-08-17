"""Document classification and tagging.

The gate that keeps the corpus clean: only iteria's own narrative responses ever
reach the retrieval index. Client RFPs are inputs, not answers. Competitor
responses, pricing sheets, and administrative paperwork never influence a draft.
Validated against the real 202-file SharePoint corpus.
"""
from __future__ import annotations

import os
import re

from .vocabulary import DEFAULT_MODULE, MODULES, SECTIONS

ITERIA_NARRATIVE = "ITERIA_NARRATIVE"
CLIENT_RFP = "CLIENT_RFP"
COMPETITOR = "COMPETITOR"
PRICING = "PRICING"
ADMIN = "ADMIN"
DEMO = "DEMO"
EXCLUDE = "EXCLUDE"
RELEASE_NOTE = "RELEASE_NOTE"
UNCLASSIFIED = "UNCLASSIFIED"

_COMPETITOR = re.compile(
    r"canam|drivestream|drive ?stream|teller|virrantech|opal|mythics|"
    r"oracle (indirect|america|response|final|demo)", re.I)
_ADMIN = re.compile(
    r"w[ -]?9\b|e[ -]?verify|non collusion|noncollusion|insurance|certificat|notari|"
    r"disclosure of ownership|confidentiality|ownership form|bidders? list", re.I)
_PRICING = re.compile(
    r"pricing|price proposal|cost worksheet|cost of ownership|\bbom\b|fee schedule", re.I)
_DEMO = re.compile(r"demo script|click ?thru|clickthru|demo\d{3}", re.I)
_JUNK = re.compile(r"\.(m97mqk|tmp|ds_store)$|\.pptx\.[a-z0-9]{6}$", re.I)
_RFP = re.compile(
    r"\brfp\b|\brfi\b|request for proposal|request for information|requirement|"
    r"solicitation|statement of work|\bsow\b|addendum|exhibit|attachment [a-z0-9]|"
    r"appendix|questionnaire|specification", re.I)
_NARRATIVE = re.compile(
    r"proposal|response|technical|executive|summary|narrative|written", re.I)

_TEXT_EXT = (".docx", ".doc", ".pdf")


def classify_path(path: str, filename: str | None = None) -> str:
    """Decide what a document IS from its path and name."""
    raw = (path or filename or "").lower()
    name = os.path.basename(raw)
    ext = os.path.splitext(name)[1]
    # Underscores and hyphens are word characters to regex \b, which would hide
    # "rfp" in "city_rfp_2026" and "w-9" in "w-9_form". Normalise separators to
    # spaces for keyword matching while keeping the real extension.
    probe = re.sub(r"[_\-]+", " ", raw)
    name_probe = re.sub(r"[_\-]+", " ", name)

    if _JUNK.search(name):
        return EXCLUDE
    if _ADMIN.search(name_probe):
        return ADMIN
    if _PRICING.search(name_probe):
        return PRICING
    if _COMPETITOR.search(probe):
        return COMPETITOR
    if _DEMO.search(probe) or ext == ".pptx":
        return DEMO
    if "iteria" in probe and ext in _TEXT_EXT and _NARRATIVE.search(probe):
        return ITERIA_NARRATIVE
    if _RFP.search(probe):
        return CLIENT_RFP
    return UNCLASSIFIED


# ---------------------------------------------------------------------------
# Body-text classification
#
# classify_path reads the filename, which is not enough and was never enough.
# All nine documents in the seed library return UNCLASSIFIED from it, because
# none of the real filenames contain the word "iteria" — the rule was written
# against a SharePoint export whose names happened to.
#
# The body is decisive, and the tell is grammatical rather than topical. An
# agency instructs: "Proposer shall submit", "the City reserves the right".
# A vendor asserts: "iteria will", "our team". West Fargo Attachment C2 reads
# as a proposal by its name and its topic, and is in fact the agency's own cost
# instructions. It is the only document in the library where agency voice
# outweighs vendor voice, at 55 against 49 per ten thousand words; everything
# else runs 45 to 279 vendor against 0 to 32 agency. Left in the retrieval
# corpus it teaches the model to write like a procurement office.
# ---------------------------------------------------------------------------
_AGENCY_VOICE = re.compile(
    r"\b(proposer|offeror|bidder|respondent)s?\b\s*"
    r"(shall|must|is instructed|are instructed|will|should)|"
    r"\bshall (submit|provide|include|complete|comply)\b|"
    r"\bthe (city|county|authority|district|agency) (reserves|shall|will)\b|"
    r"\bfailure to (comply|submit|provide)\b|"
    r"\brfp (requirements?|instructions?|schedule)\b", re.I)
_VENDOR_VOICE = re.compile(
    r"\biteria\b|\bwe (will|have|propose|understand|recommend)\b|"
    r"\bour (team|approach|proposal|consultants)\b", re.I)

# Below this there is not enough text for the rates to be stable, and the
# filename is the better of two weak signals.
_MIN_WORDS_FOR_VOICE = 200


def voice_rates(text: str) -> tuple[float, float]:
    """(agency, vendor) matches per ten thousand words."""
    words = max(len(text.split()), 1)
    return (len(_AGENCY_VOICE.findall(text)) * 10000 / words,
            len(_VENDOR_VOICE.findall(text)) * 10000 / words)


def classify(filename: str, text: str | None = None) -> str:
    """What a document is, from its name and its body.

    The name settles the categories that are about provenance rather than
    content — junk, forms, competitor material, demo scripts, price sheets —
    because a competitor's proposal reads exactly like a proposal. The body
    then settles the one distinction that matters for retrieval: whether iteria
    wrote it, or whether it was written at iteria.
    """
    by_path = classify_path(filename)
    if by_path in (EXCLUDE, ADMIN, PRICING, COMPETITOR, DEMO):
        return by_path
    if not text or len(text.split()) < _MIN_WORDS_FOR_VOICE:
        return by_path

    agency, vendor = voice_rates(text)
    if agency >= vendor:
        return CLIENT_RFP
    if vendor > agency * 1.5:
        return ITERIA_NARRATIVE
    return by_path


_MODULE_PATTERNS = [
    ("PAYROLL", r"payroll|time (and|&) attendance|timekeep|garnish|w-2|earnings code|deduction"),
    ("HCM", r"human resource|\bhr\b|talent|recruit|onboard|benefits admin|position management|"
            r"core hr|employee self|absence manage|personnel action"),
    ("FIN", r"general ledger|\bgl\b|accounts payable|\bap\b|accounts receivable|\bar\b|"
            r"chart of account|journal entr|treasury|cash manage|financial report|fund account"),
    ("BUDGET", r"budget|appropriat|encumbr"),
    ("PROC", r"procure|purchas|sourcing|requisition|purchase order|supplier|vendor manage|\bbid\b"),
    ("INV", r"inventory|fixed asset|asset manage|warehouse|stock level"),
    ("TECH", r"architect|integrat|\bapi\b|single sign|\bsso\b|security model|infrastructure|"
             r"data conversion|data migration|reporting tool|hosting|disaster recovery|cloud|interface"),
]
_MODULE_RE = [(tag, re.compile(pattern, re.I)) for tag, pattern in _MODULE_PATTERNS]

# Patterns may only emit tags the vocabulary allows, or the database rejects the
# insert at runtime with ORA-02290. Checked at import so a bad pattern is a
# startup failure rather than a failed ingest halfway through a corpus.
_unknown = {tag for tag, _ in _MODULE_PATTERNS} - set(MODULES)
if _unknown:
    raise RuntimeError(f"module patterns emit tags absent from vocabulary: {sorted(_unknown)}")


def module_of(text: str) -> str:
    """Strongest module signal in a passage. Counts hits so a passage that merely
    mentions a term in passing does not outrank one that is genuinely about it."""
    best, best_hits = DEFAULT_MODULE, 0
    for tag, pattern in _MODULE_RE:
        hits = len(pattern.findall(text))
        if hits > best_hits:
            best, best_hits = tag, hits
    return best


_SECTION_PATTERNS = [
    ("transmittal", r"cover letter|transmittal"),
    ("exec_summary", r"executive summary|exective summary"),
    ("qualifications", r"qualif|company (background|overview|profile)|about (iteria|us|our)|"
                       r"who we are|firm (background|overview)|corporate|experience"),
    ("solution", r"proposed solution|our solution|solution overview|why iteria|value propos"),
    ("methodology", r"methodolog|implementation approach|implementation plan|project (plan|approach)|"
                    r"work plan|deployment|migration approach|phases"),
    ("project_mgmt", r"project manage|governance|timeline|schedule|milestone|risk manage"),
    ("staffing", r"staff|project team|key personnel|resume|organization chart|roles and"),
    ("references", r"reference|past performance|case stud|client success"),
    ("technical", r"technical (response|requirement|approach|architecture)|architecture|"
                  r"integration|security|data conversion|hosting|reporting"),
    ("support", r"support|maintenance|training|warranty|service level|\bsla\b|help desk|managed service"),
    ("cost", r"cost proposal|pricing|price proposal|fee schedule|investment"),
    ("contract", r"contract|terms and condition|agreement|exceptions"),
    ("compliance", r"compliance|mandatory requirement|requirement matrix"),
]
_SECTION_RE = [(tag, re.compile(pattern, re.I)) for tag, pattern in _SECTION_PATTERNS]

_unknown = {tag for tag, _ in _SECTION_PATTERNS} - set(SECTIONS)
if _unknown:
    raise RuntimeError(f"section patterns emit tags absent from vocabulary: {sorted(_unknown)}")


def section_of(heading: str) -> str | None:
    for tag, pattern in _SECTION_RE:
        if pattern.search(heading):
            return tag
    return None
