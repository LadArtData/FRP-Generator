"""
HARALD slots.

The structure of a proposal is data, not model output. This module owns
that data and the gate that decides whether a slot may be written at all.

Why it exists, stated plainly: a single model pass asked to fill a
template and then report what is missing will always choose filling. From
inside that task an empty slot reads as failure, so the model invents
something to put there. Demo data, plausible dates, references that do not
exist. No prompt fixes it, because the two jobs are in genuine conflict.

So the jobs are separated:

  STRUCTURE is code.        SlotPlan is built by a parser and walked by the
                            assembler. No model touches it. It cannot
                            invent a section because it has no generative
                            capacity.

  CONTENT is gated.         Each slot runs retrieval first. Clear the floor
                            and the model writes that slot from those
                            excerpts. Fail the floor and the model is never
                            called. The slot stays empty.

  THE CHECKLIST is computed. It is the list of slots that failed, derived
                            from slot state by the assembler. Nobody writes
                            it, so it cannot be optimistic.

The model never sees the plan, never sees the other slots, and never
knows how much is unfilled. It has nothing to be complete about.

Retrieval and generation are injected as callables so this module has no
dependency on a live database or a live API, and so the gate can be tested
against fixtures. The real wiring is in harald_db.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Optional

from . import tagger
from .vocabulary import MODULES, SECTIONS


class SlotState(str, Enum):
    PENDING = "pending"          # not yet attempted
    FILLED = "filled"            # written and passed its checks
    NEEDS_REVIEW = "needs_review"  # written but failed a check
    NO_SOURCE = "no_source"      # retrieval floor not met; never sent to a model
    CONSULTANT = "consultant"    # marked by the plan as human-only
    SKIPPED = "skipped"          # not applicable to this RFP


# ----------------------------------------------------------------------
# The floor. A slot is only written when retrieval gives the model enough
# real material to write from.
#
# These defaults are a starting point, not a calibration. They must be
# tuned against real harald_match output: run the RFP's requirements
# through retrieval and look at where the returns actually go thin. The
# numbers below are deliberately conservative, because the failure they
# guard against is fabrication.
# ----------------------------------------------------------------------
@dataclass
class RetrievalFloor:
    min_chunks: int = 3
    min_top_score: float = 0.0        # score scale is Oracle Text SCORE(1) weighted
    min_total_tokens: int = 250
    require_module_specific: bool = True   # at least one non-GENERAL chunk
    require_verified: bool = True          # at least one trust_level=VERIFIED

    def evaluate(self, excerpts: list[dict]) -> tuple[bool, list[str]]:
        """Returns (passed, reasons_for_failure)."""
        reasons = []
        if len(excerpts) < self.min_chunks:
            reasons.append(
                f"only {len(excerpts)} excerpt(s) retrieved, floor is {self.min_chunks}")
        if excerpts:
            top = max(float(e.get("rank_score") or 0) for e in excerpts)
            if top <= self.min_top_score:
                reasons.append(
                    f"best match scored {top:.4f}, floor is above {self.min_top_score}")
        total = sum(int(e.get("token_count") or _est(e)) for e in excerpts)
        if total < self.min_total_tokens:
            reasons.append(
                f"{total} tokens of source material, floor is {self.min_total_tokens}")
        if self.require_module_specific and not any(
                (e.get("module_tag") or "GENERAL") not in ("GENERAL",)
                for e in excerpts):
            reasons.append("no module-specific source, only general narrative")
        if self.require_verified and not any(
                (e.get("trust_level") or "VERIFIED").upper() == "VERIFIED"
                for e in excerpts):
            reasons.append("no verified source material")
        return (not reasons), reasons


def _est(excerpt: dict) -> int:
    t = excerpt.get("chunk_text") or excerpt.get("text") or ""
    return max(1, round(len(t) / 4))


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------
@dataclass
class Slot:
    slot_id: str
    ordinal: int
    title: str                       # exactly as the RFP words it
    requirement_text: str = ""       # the requirement this slot answers
    section_tag: str = "general"     # drives retrieval
    module_tag: Optional[str] = None
    rfp_reference: str = ""          # section number in the client's document
    page_limit: Optional[int] = None
    consultant_only: bool = False    # forms, signatures, pricing tables
    required: bool = True

    def retrieval_key(self) -> tuple[Optional[str], Optional[str]]:
        """
        (section, module) to filter retrieval on. 'general' is the
        tagger's fallback, not a real section, so it is never used as a
        filter: doing so excludes the whole library.
        """
        section = self.section_tag if self.section_tag != "general" else None
        return section, self.module_tag

    state: SlotState = SlotState.PENDING
    text: str = ""
    excerpt_ids: list = field(default_factory=list)
    floor_failures: list = field(default_factory=list)
    check_report: dict = field(default_factory=dict)

    def as_dict(self):
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class SlotPlan:
    client_name: str
    rfp_number: str
    slots: list[Slot] = field(default_factory=list)

    def add(self, **kw) -> Slot:
        kw.setdefault("ordinal", len(self.slots) + 1)
        kw.setdefault("slot_id", f"S{kw['ordinal']:03d}")
        if kw.get("section_tag") and kw["section_tag"] not in SECTIONS:
            raise ValueError(f"unknown section_tag {kw['section_tag']!r}; "
                             f"must be one of {SECTIONS}")
        if kw.get("module_tag") and kw["module_tag"] not in MODULES:
            raise ValueError(f"unknown module_tag {kw['module_tag']!r}; "
                             f"must be one of {MODULES}")
        s = Slot(**kw)
        self.slots.append(s)
        return s

    def by_state(self, state: SlotState) -> list[Slot]:
        return [s for s in self.slots if s.state == state]

    def to_json(self) -> str:
        return json.dumps({
            "client_name": self.client_name,
            "rfp_number": self.rfp_number,
            "slots": [s.as_dict() for s in self.slots],
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "SlotPlan":
        d = json.loads(raw)
        plan = cls(client_name=d["client_name"], rfp_number=d["rfp_number"])
        for sd in d["slots"]:
            sd = dict(sd)
            sd["state"] = SlotState(sd.get("state", "pending"))
            plan.slots.append(Slot(**sd))
        return plan


# ----------------------------------------------------------------------
# Building a plan from a parsed RFP
# ----------------------------------------------------------------------
# Titles that are always a human's job. A model cannot sign a form or set
# a price, and letting it try is exactly how demo data got into documents.
_CONSULTANT_ONLY = re.compile(
    r"\bpricing\b|\bcost (proposal|form|sheet|schedule)\b|fee schedule|"
    r"\bw-?9\b|e-?verify|non-?collusion|insurance certificate|"
    r"signature|signed|notari|affidavit|acknowledg?ement of addend|"
    r"bid bond|surety|references? form|reference contact", re.I)

# Map an RFP's heading to the library's section vocabulary
_TITLE_TO_SECTION = [
    ("transmittal",    r"cover letter|transmittal|letter of interest"),
    ("exec_summary",   r"executive (summary|narrative)|overview of (the )?proposal"),
    ("qualifications", r"qualification|company (background|profile|history)|"
                       r"firm (experience|information)|corporate experience|about"),
    ("methodology",    r"approach|methodolog|implementation plan|work plan|"
                       r"project plan|scope of (work|services)"),
    ("project_mgmt",   r"project management|governance|schedule|timeline|"
                       r"change (control|management)"),
    ("staffing",       r"staffing|key personnel|project team|resume|"
                       r"organization chart|personnel"),
    ("references",     r"reference|past performance|client list|case stud"),
    ("technical",      r"technical|architecture|integration|security|"
                       r"data (migration|conversion)|system requirement|hosting"),
    ("support",        r"support|training|maintenance|warranty|help ?desk|"
                       r"knowledge transfer|post.?implementation"),
    ("cost",           r"cost|pricing|price|fee|payment|invoice"),
    ("risk",           r"risk|mitigation|contingency|assumption"),
    ("contract",       r"terms|conditions|contract|exception|insurance|"
                       r"legal|compliance|addend|certification"),
]
_TITLE_RES = [(t, re.compile(p, re.I)) for t, p in _TITLE_TO_SECTION]


def section_for_title(title: str) -> str:
    for tag, rx in _TITLE_RES:
        if rx.search(title):
            return tag
    return "general"


def module_for_title(title: str) -> Optional[str]:
    """
    Infer the module from the RFP's own heading. A requirement headed
    "Human Capital Management Requirements" is an HCM requirement whether
    or not the parser said so, and without this it retrieves against
    section='general' and finds nothing usable.
    """
    tag, score = tagger.tag_module(title)
    return tag if tag not in ("GENERAL",) and score > 0 else None


def plan_from_requirements(client_name: str, rfp_number: str,
                           requirements: list[dict]) -> SlotPlan:
    """
    requirements: ordered list of dicts from RFP parsing, each with at
    minimum {"title": ...}. Optional keys: "text", "reference",
    "page_limit", "module", "required".

    Every requirement becomes exactly one slot, in the RFP's own order,
    under the RFP's own heading. The document that gets assembled has the
    shape the client asked for, because the shape came from the client.
    """
    plan = SlotPlan(client_name=client_name, rfp_number=rfp_number)
    for r in requirements:
        title = (r.get("title") or "").strip()
        if not title:
            raise ValueError(f"requirement with no title: {r!r}")
        plan.add(
            title=title,
            requirement_text=(r.get("text") or title).strip(),
            section_tag=section_for_title(title),
            module_tag=r.get("module") or module_for_title(title),
            rfp_reference=(r.get("reference") or "").strip(),
            page_limit=r.get("page_limit"),
            consultant_only=bool(_CONSULTANT_ONLY.search(title)),
            required=bool(r.get("required", True)),
        )
    return plan


# ----------------------------------------------------------------------
# The assembler
# ----------------------------------------------------------------------
@dataclass
class AssemblyResult:
    plan: SlotPlan
    filled: int
    needs_review: int
    no_source: int
    consultant: int

    @property
    def coverage(self) -> float:
        answerable = [s for s in self.plan.slots if not s.consultant_only]
        if not answerable:
            return 0.0
        done = sum(1 for s in answerable
                   if s.state in (SlotState.FILLED, SlotState.NEEDS_REVIEW))
        return done / len(answerable)


def assemble(plan: SlotPlan,
             retrieve: Callable[[Slot], list[dict]],
             generate: Callable[[Slot, list[dict]], dict],
             floor: RetrievalFloor | None = None,
             progress: Optional[Callable[[Slot], None]] = None) -> AssemblyResult:
    """
    Walk the plan. One slot at a time, gate first.

    retrieve(slot) -> list of excerpt dicts, each with at least chunk_id,
                      chunk_text, module_tag, trust_level, rank_score.
    generate(slot, excerpts) -> dict from harald_generate.generate_section.

    generate is only ever called for a slot that cleared the floor, and it
    is called with that slot alone. It is never shown the plan.
    """
    floor = floor or RetrievalFloor()

    for slot in plan.slots:
        if slot.consultant_only:
            slot.state = SlotState.CONSULTANT
            slot.floor_failures = ["human-only: forms, pricing or signature"]
            if progress:
                progress(slot)
            continue

        excerpts = retrieve(slot) or []
        passed, reasons = floor.evaluate(excerpts)

        if not passed:
            slot.state = SlotState.NO_SOURCE
            slot.floor_failures = reasons
            slot.excerpt_ids = [e.get("chunk_id") for e in excerpts]
            slot.text = ""
            if progress:
                progress(slot)
            continue

        result = generate(slot, excerpts)
        slot.text = result.get("final_clob") or ""
        slot.excerpt_ids = json.loads(result.get("excerpts_used") or "[]") \
            if isinstance(result.get("excerpts_used"), str) \
            else (result.get("excerpts_used") or [])
        report = result.get("check_report") or {}
        if isinstance(report, str):
            report = json.loads(report)
        slot.check_report = report
        slot.state = (SlotState.FILLED if report.get("clean")
                      else SlotState.NEEDS_REVIEW)
        if progress:
            progress(slot)

    return AssemblyResult(
        plan=plan,
        filled=len(plan.by_state(SlotState.FILLED)),
        needs_review=len(plan.by_state(SlotState.NEEDS_REVIEW)),
        no_source=len(plan.by_state(SlotState.NO_SOURCE)),
        consultant=len(plan.by_state(SlotState.CONSULTANT)),
    )


# ----------------------------------------------------------------------
# The checklist. Computed from slot state. Nobody writes this.
# ----------------------------------------------------------------------
def checklist(result: AssemblyResult) -> list[dict]:
    rows = []
    for s in result.plan.slots:
        if s.state == SlotState.FILLED:
            continue
        if s.state == SlotState.CONSULTANT:
            action = "Complete by hand. Forms, pricing and signatures are not generated."
        elif s.state == SlotState.NO_SOURCE:
            action = ("Write from scratch, or add source material to the library. "
                      "Nothing was generated because: " + "; ".join(s.floor_failures))
        elif s.state == SlotState.NEEDS_REVIEW:
            r = s.check_report
            bits = []
            if r.get("placeholders_dropped"):
                bits.append(f"{len(r['placeholders_dropped'])} placeholder(s) lost")
            if r.get("unsourced_numbers"):
                bits.append("unsourced figures: " + ", ".join(r["unsourced_numbers"]))
            if r.get("banned_words"):
                bits.append(f"{len(r['banned_words'])} flagged word(s)")
            if r.get("forbidden_chars"):
                bits.append("dash characters present")
            if r.get("rhythm_flat"):
                bits.append("sentence rhythm too even")
            action = "Review the draft. " + ("; ".join(bits) if bits else "Failed style check.")
        else:
            action = "Not attempted."
        rows.append({
            "slot_id": s.slot_id,
            "ordinal": s.ordinal,
            "rfp_reference": s.rfp_reference,
            "title": s.title,
            "state": s.state.value,
            "required": s.required,
            "action": action,
        })
    return rows


def render_checklist(result: AssemblyResult) -> str:
    rows = checklist(result)
    lines = [
        f"HARALD assembly, {result.plan.client_name}, {result.plan.rfp_number}",
        "",
        f"  {len(result.plan.slots)} slots in the RFP structure",
        f"  {result.filled} written and clean",
        f"  {result.needs_review} written, needs review",
        f"  {result.no_source} not written, no source material",
        f"  {result.consultant} for a human by design",
        f"  coverage of answerable slots: {result.coverage:.0%}",
        "",
    ]
    if not rows:
        lines.append("Nothing outstanding.")
        return "\n".join(lines)
    lines.append("OUTSTANDING")
    for r in rows:
        flag = "" if r["required"] else "  (optional)"
        ref = f" [{r['rfp_reference']}]" if r["rfp_reference"] else ""
        lines.append(f"  {r['ordinal']:>3}. {r['title']}{ref}{flag}")
        lines.append(f"       {r['state']}: {r['action']}")
    return "\n".join(lines)
