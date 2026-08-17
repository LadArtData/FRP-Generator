"""Classify solicitation type so HAROLD drafts the right shape of response."""
from __future__ import annotations

import re
from dataclasses import dataclass

_ERP_HINTS = (
    "oracle", "erp", "fusion", "financials", "hcm", "payroll", "procurement",
    "general ledger", "cloud implementation", "r12", "e-business", "ebs",
)
_AI_HINTS = (
    "artificial intelligence", " ai ", "machine learning", "ml ", "enablement",
    "generative ai", "genai", "governance", "prompt engineering", "llm",
    "copilot", "data science", "analytics platform",
)
_HEALTH_HINTS = ("health", "hipaa", "hospital", "clinical", "patient", "ttuhsc")


@dataclass(frozen=True)
class EngagementProfile:
    kind: str  # erp_modernization | ai_enablement | general_consulting
    label: str
    writing_focus: str


def _hint_score(blob: str, hints: tuple[str, ...]) -> int:
    score = 0
    for hint in hints:
        h = hint.strip()
        if len(h) <= 4:
            if re.search(rf"\b{re.escape(h)}\b", blob):
                score += 1
        elif h in blob:
            score += 1
    return score


def classify_text(*parts: str | None) -> EngagementProfile:
    blob = " ".join(p for p in parts if p).lower()
    erp = _hint_score(blob, _ERP_HINTS)
    ai = _hint_score(blob, _AI_HINTS)
    health = any(h in blob for h in _HEALTH_HINTS)

    if ai >= 2 and erp == 0:
        focus = (
            "Write as an AI adoption and enablement consulting response. Emphasize "
            "assessment, roadmap, governance, training, HIPAA-aware patterns for "
            "healthcare clients, and phased enablement — not a generic ERP rollout."
        )
        if health:
            focus += " Tie recommendations to clinical and administrative workflows at a health sciences center."
        return EngagementProfile("ai_enablement", "Enterprise AI adoption and enablement", focus)

    if erp >= 2:
        return EngagementProfile(
            "erp_modernization",
            "Oracle Cloud Fusion ERP modernization",
            "Write as an Oracle Cloud Fusion implementation response. Name modules, "
            "integrations, conversion, testing, and public-sector controls.",
        )

    if ai >= 1 and erp >= 1:
        return EngagementProfile(
            "mixed",
            "Oracle Cloud plus AI enablement",
            "Blend Fusion implementation substance with AI governance and enablement. "
            "Separate ERP facts from AI advisory facts.",
        )

    return EngagementProfile(
        "general_consulting",
        "Public-sector consulting services",
        "Write as a consulting services response grounded in iteria's Oracle and "
        "enablement portfolio. Stay concrete; mark unknowns [NEEDS HUMAN: ...].",
    )


def classify_opportunity(parsed_fields: dict | None, rfp_text: str = "") -> EngagementProfile:
    fields = parsed_fields or {}
    modules = fields.get("required_modules") or fields.get("proposed_modules") or []
    if isinstance(modules, list):
        mod_text = " ".join(str(m) for m in modules)
    else:
        mod_text = str(modules)
    return classify_text(
        fields.get("title"),
        fields.get("pain_points"),
        fields.get("industry"),
        mod_text,
        rfp_text[:4000],
    )


def default_modules(profile: EngagementProfile) -> list[str]:
    if profile.kind == "ai_enablement":
        return ["TECH", "GENERAL"]
    if profile.kind == "erp_modernization":
        return ["FIN", "HCM", "PAYROLL", "PROC", "TECH"]
    return ["TECH", "GENERAL"]
