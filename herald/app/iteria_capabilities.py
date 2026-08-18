"""Iteria capability baseline — always available even when the library misses.

HAROLD's retrieval index holds past proposal language. This module holds what
iteria actually sells and how we talk about it when no exemplar exists. It is
injected into every draft/QA path so a thin library never becomes a decline.
"""
from __future__ import annotations

from .engagement import EngagementProfile

# Canonical services — update here when iteria's portfolio changes.
ITERIA_SERVICES = """
iteria is an Oracle Cloud Fusion implementation and consulting partner serving
public-sector, healthcare, and higher-education clients.

What iteria delivers:
- Oracle Cloud ERP: Financials, HCM, Payroll, Procurement, Budget, Projects, SCM
- Technical approach: integrations (REST/SOAP, OIC), data conversion, reporting/BI,
  security and role design, environment strategy, cutover, hypercare
- Change management, end-user training, center-of-excellence setup, managed services
- AI adoption and enablement: readiness assessments, use-case prioritization,
  governance operating models, workflow automation, staff training, responsible AI
  patterns in regulated environments (including HIPAA-aware healthcare contexts)
- Fixed-fee and T&M consulting, phase-gated delivery, executive steering cadence

How iteria works:
- Lead with named mechanisms (workshops, fit-gap, CRP cycles, conference-room pilots)
- Ground claims in Oracle product capability + iteria delivery method — never invent
  client metrics, certifications, or go-live dates
- Mark unknown client facts as [NEEDS HUMAN: ...] for consultant completion
- Never answer that iteria cannot meet a requirement solely because the library
  lacks an exemplar
"""

ERP_SECTIONS: list[tuple[str, str | None]] = [
    ("Executive Summary", None),
    ("Understanding of Requirements", None),
    ("Technical Approach", "TECH"),
    ("Financial Management", "FIN"),
    ("Human Resources and Payroll", "HCM"),
    ("Procurement and Supply Chain", "PROC"),
    ("Implementation Methodology", None),
    ("Project Management and Governance", "TECH"),
    ("Training and Change Management", "HCM"),
    ("Support and Managed Services", "TECH"),
]

AI_SECTIONS: list[tuple[str, str | None]] = [
    ("Executive Summary", None),
    ("Understanding of Client Goals", None),
    ("Current-State Assessment Approach", "TECH"),
    ("Enterprise AI Strategy and Roadmap", "TECH"),
    ("Governance, Security, and Compliance", "TECH"),
    ("Training, Enablement, and Change Management", "HCM"),
    ("Implementation and Transition Support", "TECH"),
    ("Project Team and Qualifications", None),
    ("Project Management Approach", "TECH"),
]

GENERAL_SECTIONS: list[tuple[str, str | None]] = [
    ("Executive Summary", None),
    ("Understanding of Requirements", None),
    ("Technical Approach", "TECH"),
    ("Implementation Methodology", None),
    ("Project Management and Governance", "TECH"),
    ("Team Qualifications", None),
]


def context_for(profile: EngagementProfile) -> str:
    lines = [
        "ITERIA CAPABILITY BASELINE (authoritative when library is thin):",
        ITERIA_SERVICES.strip(),
        f"Engagement profile for this bid: {profile.label}.",
        profile.writing_focus,
    ]
    return "\n".join(lines)


def section_plan(profile: EngagementProfile) -> list[tuple[str, str | None]]:
    if profile.kind == "ai_enablement":
        return list(AI_SECTIONS)
    if profile.kind == "erp_modernization":
        return list(ERP_SECTIONS)
    return list(GENERAL_SECTIONS)
