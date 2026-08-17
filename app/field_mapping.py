"""Translate solicitation-parsed fields into Studio form vocabulary.

The parser (``prompts.RFP_PARSE_SYSTEM``) emits what the document says:
``industry`` as free text, ``engagement_type`` as a snake_case code,
``due_date`` however the agency wrote it, ``pain_points`` as a sentence. The
Studio form speaks a different, closed vocabulary: fixed ``<select>`` options,
an ``input[type=date]`` that only accepts ISO, and a seven-item checkbox list.

Nothing used to sit between those two vocabularies. ``studio.parse`` wrote
``parsed_fields`` and stopped, and the form kept whatever it already held, so a
stale value outlived the solicitation that contradicted it — and because every
consumer merges ``studio_form`` *over* ``parsed_fields``, the stale half won.
This module is that missing translation layer, and ``reconcile`` is the rule
for which half wins when they disagree.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Closed vocabularies. These mirror web/index.html exactly; a value that is not
# one of these cannot be represented in the form, so mapping must land on one
# of them or on "" (meaning "leave for a human").
# ---------------------------------------------------------------------------

INDUSTRY_OPTIONS = (
    "City / Municipality",
    "County Government",
    "State Agency",
    "Federal",
    "K-12 / Higher Ed",
    "Healthcare / Health Sciences",
    "Utility / Special District",
)

ENGAGEMENT_OPTIONS = (
    "Full implementation",
    "Upgrade",
    "AI enablement & consulting",
    "Advisory",
    "Staff augmentation",
)

PAIN_POINT_OPTIONS = (
    "Manual processes",
    "Data silos",
    "Aging system",
    "Compliance gaps",
    "Poor reporting",
    "Vendor support ending",
    "High maintenance cost",
)

MODULE_OPTIONS = (
    "Financials", "Procurement", "HCM", "Payroll",
    "EPM", "SCM", "Project Portfolio", "Analytics / OAC",
)

# Fields the solicitation is authoritative about. Autofill may overwrite these,
# because the user asked for the document's version of them.
SOLICITATION_FIELDS = (
    "client_name", "industry", "primary_contact", "annual_budget",
    "legacy_systems", "rfp_number", "due_date", "pain_points",
    "pain_points_text", "proposed_modules", "engagement_type", "services_scope",
)

# Fields only a human writes. Autofill must never touch these — but when the
# solicitation contradicts one, say so rather than silently leaving it.
HUMAN_AUTHORED_FIELDS = (
    "win_theme", "project_manager", "solution_architect", "primary_competition",
    "sso_platform", "compliance_frameworks", "legacy_convert",
)


# ---------------------------------------------------------------------------
# Individual field mappers
# ---------------------------------------------------------------------------

_INDUSTRY_PATTERNS = (
    # Order matters: health sciences centres are also higher ed, and the
    # clinical mission is the one that shapes the response.
    (r"health|hospital|clinical|medical|hipaa|patient|hsc\b", "Healthcare / Health Sciences"),
    (r"school district|k-?12|university|college|higher ed|campus|academic",
     "K-12 / Higher Ed"),
    (r"\bcounty\b|sheriff|parish", "County Government"),
    (r"\bstate\b(?!\s*of\s*the)|commonwealth", "State Agency"),
    (r"federal|\bgsa\b|\bdod\b|\bva\b\b", "Federal"),
    (r"utility|water district|transit authority|special district|airport|port authority",
     "Utility / Special District"),
    (r"\bcity\b|\btown\b|\bvillage\b|munic|borough", "City / Municipality"),
)

_ENGAGEMENT_PATTERNS = (
    (r"ai[_ -]?enablement|artificial intelligence|generative ai|ai adoption|"
     r"ai readiness|machine learning", "AI enablement & consulting"),
    (r"erp[_ -]?modernization|full implementation|implementation|deployment|"
     r"modernization", "Full implementation"),
    (r"upgrade|migration|reimplementation", "Upgrade"),
    (r"staff[_ -]?aug|augmentation|contingent|resource", "Staff augmentation"),
    (r"advisory|assessment|roadmap|general[_ -]?consulting|consulting|strategy",
     "Advisory"),
)

_PAIN_POINT_PATTERNS = (
    (r"manual|paper[- ]?based|spreadsheet|re-?key", "Manual processes"),
    (r"silo|fragmented|disconnected|integration gap", "Data silos"),
    (r"aging|legacy|end[- ]of[- ]life|outdated|obsolete", "Aging system"),
    (r"complian|audit|regulat|hipaa|control gap", "Compliance gaps"),
    (r"reporting|visibility|analytics gap|dashboard", "Poor reporting"),
    (r"support ending|sunset|unsupported|de-?support|extended support",
     "Vendor support ending"),
    (r"maintenance cost|cost of ownership|expensive to maintain|licensing cost",
     "High maintenance cost"),
)

_MODULE_PATTERNS = (
    (r"financial|general ledger|\bgl\b|\bap\b|\bar\b|accounting", "Financials"),
    (r"procure|purchas|\bp2p\b|sourcing|contract manage", "Procurement"),
    (r"\bhcm\b|human (resource|capital)|\bhr\b|talent|benefits", "HCM"),
    (r"payroll|time and attendance|\bt&a\b", "Payroll"),
    (r"\bepm\b|budget|planning|forecast", "EPM"),
    (r"\bscm\b|supply chain|inventory|warehouse", "SCM"),
    (r"project portfolio|\bppm\b|grants|capital project", "Project Portfolio"),
    (r"analytic|\boac\b|business intelligence|\bbi\b|reporting platform",
     "Analytics / OAC"),
)


def _blob(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v) for v in value).lower()
    return str(value).lower()


def map_industry(value, *, fallback_text: str = "") -> str:
    """Free-text industry -> one of INDUSTRY_OPTIONS, or "" if undecidable.

    ``fallback_text`` lets the caller widen the evidence (client name, RFP body)
    when the parsed industry alone is ambiguous or absent.
    """
    for source in (_blob(value), _blob(fallback_text)):
        if not source:
            continue
        for pattern, option in _INDUSTRY_PATTERNS:
            if re.search(pattern, source):
                return option
    return ""


def map_engagement_type(value, *, fallback_text: str = "") -> str:
    """Parser code or free text -> one of ENGAGEMENT_OPTIONS, or ""."""
    for source in (_blob(value), _blob(fallback_text)):
        if not source:
            continue
        for pattern, option in _ENGAGEMENT_PATTERNS:
            if re.search(pattern, source):
                return option
    return ""


_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%m-%d-%Y",
)


def map_due_date(value) -> str:
    """Any reasonable date spelling -> ISO ``YYYY-MM-DD`` for ``input[type=date]``.

    An ``input[type=date]`` silently discards anything that is not ISO, which is
    how "September 21, 2026" became an empty due date on a bid.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return ""
    # Drop ordinal suffixes and any trailing time-of-day.
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    cleaned = re.sub(r"\s+at\s+.*$", "", cleaned, flags=re.I)
    # strptime's %b wants "Sep"; agencies write "Sept". Same for the other
    # four-letter abbreviations people actually type.
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.I)
    cleaned = re.sub(r"\bTues\b", "Tue", cleaned, flags=re.I)
    cleaned = cleaned.strip().rstrip(",.")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    # Last resort: a bare ISO date embedded in a longer string.
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", cleaned)
    if match:
        try:
            return date(*(int(g) for g in match.groups())).isoformat()
        except ValueError:
            return ""
    return ""


def map_pain_points(value) -> tuple[list[str], str]:
    """-> (checkbox labels that matched, original text preserved verbatim).

    The checkbox list is closed and cannot express "AI adoption and enablement".
    Returning the free text alongside is what stops that from being dropped: the
    caller stores it in ``pain_points_text`` so the brief still carries it.
    """
    if value in (None, "", [], ()):
        return [], ""
    raw = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple, set)) else str(value)
    source = raw.lower()
    matched = [option for pattern, option in _PAIN_POINT_PATTERNS
               if re.search(pattern, source)]
    # dict.fromkeys preserves first-seen order while de-duplicating.
    return list(dict.fromkeys(matched)), raw.strip()


def map_modules(value) -> list[str]:
    """Module names in any spelling -> MODULE_OPTIONS labels."""
    if value in (None, "", [], ()):
        return []
    entries = value if isinstance(value, (list, tuple, set)) else \
        [p for p in re.split(r"[,;/]", str(value))]
    out: list[str] = []
    for entry in entries:
        source = _blob(entry)
        if not source.strip():
            continue
        for pattern, option in _MODULE_PATTERNS:
            if re.search(pattern, source):
                out.append(option)
                break
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Whole-record mapping
# ---------------------------------------------------------------------------

def to_form(parsed: dict | None, *, rfp_text: str = "") -> dict:
    """Project ``parsed_fields`` onto the Studio form's vocabulary.

    Only non-empty results are returned, so a field the solicitation is silent
    about never blanks something already on the form.
    """
    fields = parsed or {}
    client_name = str(fields.get("client_name") or "").strip()
    # Industry evidence widens from the parsed value to the client name and then
    # the document body, because "TTUHSC" alone identifies the sector when the
    # parser returned prose that matches no option.
    industry = map_industry(
        fields.get("industry"),
        fallback_text=f"{client_name} {fields.get('agency') or ''} {rfp_text[:4000]}",
    )
    engagement = map_engagement_type(
        fields.get("engagement_type"),
        fallback_text=f"{fields.get('pain_points') or ''} {rfp_text[:4000]}",
    )
    pain_list, pain_text = map_pain_points(fields.get("pain_points"))
    modules = map_modules(fields.get("required_modules") or fields.get("proposed_modules"))

    form = {
        "client_name": client_name,
        "industry": industry,
        "primary_contact": str(fields.get("primary_contact") or "").strip(),
        "annual_budget": str(fields.get("annual_budget") or "").strip(),
        "legacy_systems": str(fields.get("legacy_systems") or "").strip(),
        "rfp_number": str(fields.get("rfp_number") or "").strip(),
        "due_date": map_due_date(fields.get("due_date")),
        "engagement_type": engagement,
        "pain_points": pain_list,
        "pain_points_text": pain_text,
        "proposed_modules": modules,
    }
    return {k: v for k, v in form.items() if v not in (None, "", [], ())}


def reconcile(existing_form: dict | None,
              parsed: dict | None,
              *,
              rfp_text: str = "") -> tuple[dict, dict, list[dict]]:
    """Merge a fresh parse into the saved form.

    Returns ``(form, changed, conflicts)``.

    The rule: the solicitation wins for the fields it is authoritative about,
    because the user pressed a button labelled "Autofill from solicitation" and
    meant it. Human-authored fields are never overwritten — but where one of
    them contradicts the document, it is reported in ``conflicts`` so the UI can
    surface it instead of quietly shipping a stale win theme.
    """
    form = dict(existing_form or {})
    mapped = to_form(parsed, rfp_text=rfp_text)
    changed: dict = {}

    for key in SOLICITATION_FIELDS:
        if key not in mapped:
            continue
        if form.get(key) != mapped[key]:
            changed[key] = {"from": form.get(key), "to": mapped[key]}
        form[key] = mapped[key]

    conflicts: list[dict] = []
    win_theme = str(form.get("win_theme") or "").strip()
    if win_theme:
        engagement = mapped.get("engagement_type", "")
        theme_blob = win_theme.lower()
        # A cutover-themed win theme on an advisory or AI engagement is the
        # specific mismatch that shipped a county-ERP story to a health system.
        if engagement in ("AI enablement & consulting", "Advisory") and \
                re.search(r"cutover|go-?live|implementation|module", theme_blob):
            conflicts.append({
                "field": "win_theme",
                "value": win_theme,
                "reason": f"Win theme describes an implementation cutover, but the "
                          f"solicitation reads as {engagement}.",
            })

    for key in HUMAN_AUTHORED_FIELDS:
        form.setdefault(key, existing_form.get(key, "") if existing_form else "")

    return form, changed, conflicts
