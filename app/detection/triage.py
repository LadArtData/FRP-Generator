"""Triage classification — mirrors the Review Queue in the console."""

from __future__ import annotations

import re

from .models import PersonFinding, RuleDef

REC_PREFIX = "REC-"
RECRUITING_NAME = re.compile(r"recruit|requisition|candidate", re.I)
TIER_RANK = {"critical": 3, "high": 2, "moderate": 1, "low": 0}


def is_recruiting_rule(rule_id: str, rules: dict[str, RuleDef]) -> bool:
    if rule_id.startswith(REC_PREFIX):
        return True
    r = rules.get(rule_id)
    return bool(r and RECRUITING_NAME.search(r.name))


def is_rec_only(rule_ids: list[str], rules: dict[str, RuleDef]) -> bool:
    return bool(rule_ids) and all(is_recruiting_rule(r, rules) for r in rule_ids)


def person_top_tier(rule_ids: list[str], rules: dict[str, RuleDef]) -> str:
    best = "low"
    for rid in rule_ids:
        tier = rules.get(rid, RuleDef(rid, "", "", "")).tier
        if TIER_RANK.get(tier, 0) > TIER_RANK.get(best, 0):
            best = tier
    return best


def classify_person(
    finding: PersonFinding,
    rules: dict[str, RuleDef],
    role_scope: dict[str, str],
) -> None:
    rids = finding.rule_ids
    if not rids:
        return

    if is_rec_only(rids, rules):
        finding.bucket = "confirm"
        finding.reason = "recruiting"
        return

    if all(role_scope.get(r, "scoped") in ("department", "scoped") for r in rids):
        finding.bucket = "confirm"
        finding.reason = "aor"
        return

    top = person_top_tier(rids, rules)
    if top in ("low", "moderate"):
        finding.bucket = "confirm"
        finding.reason = "lowmat"
        return

    finding.bucket = "review"
    finding.reason = ""


def severity_label(conflict_count: int) -> str:
    if conflict_count >= 10:
        return "critical"
    if conflict_count >= 5:
        return "high"
    return "medium"
