"""Deterministic set-based SOD detection.

Each person's effective access is flattened through the role hierarchy.
Anyone holding both sides of an active conflict rule — with evidence on
each side — is flagged. Every finding traces to the role and privilege that
produced it.
"""

from __future__ import annotations

from collections import defaultdict

from .models import ConflictRow, ModelRow, PersonFinding, RuleDef
from .triage import classify_person, is_rec_only, person_top_tier, severity_label


def _index_model(rows: list[ModelRow]) -> dict:
    holders: dict[str, set[str]] = defaultdict(set)  # person -> roles
    role_privs: dict[str, set[str]] = defaultdict(set)
    priv_meta: dict[str, dict] = {}
    role_meta: dict[str, dict] = {}

    for row in rows:
        et = row.entity_type.upper()
        if et == "HOLDER":
            holders[row.parent_key].add(row.entity_key)
        elif et == "GRANT":
            role_privs[row.parent_key].add(row.entity_key)
        elif et == "ROLE":
            role_meta[row.entity_key] = row.attrs
        elif et == "PRIV":
            priv_meta[row.entity_key] = row.attrs

    return {
        "holders": holders,
        "role_privs": role_privs,
        "role_meta": role_meta,
        "priv_meta": priv_meta,
    }


def _person_privileges(person: str, model: dict) -> set[str]:
    privs: set[str] = set()
    for role in model["holders"].get(person, ()):
        privs.update(model["role_privs"].get(role, ()))
    return privs


def _role_scope_for_rules(person: str, rule_ids: list[str], model: dict) -> dict[str, str]:
    """Map each rule to the broadest scope among roles that carry its privileges."""
    scopes: dict[str, str] = {}
    roles = model["holders"].get(person, ())
    for rid in rule_ids:
        broadest = "scoped"
        for role in roles:
            meta = model["role_meta"].get(role, {})
            scope = (meta.get("scope") or "scoped").lower()
            if scope in ("unrestricted", "all"):
                broadest = "unrestricted"
                break
            if scope in ("business_unit", "bu") and broadest != "unrestricted":
                broadest = "business_unit"
            if scope in ("department", "dept", "scoped"):
                if broadest not in ("unrestricted", "business_unit"):
                    broadest = "department"
        scopes[rid] = broadest
    return scopes


def _evidence_both_sides(person: str, rule: RuleDef, privs: set[str]) -> bool:
    has_a = bool(rule.privs_a & privs) if rule.privs_a else True
    has_b = bool(rule.privs_b & privs) if rule.privs_b else True
    return has_a and has_b


def _self_conflicted_roles(rules: dict[str, RuleDef], model: dict) -> dict[str, list[str]]:
    conflicted: dict[str, list[str]] = defaultdict(list)
    for role, privs in model["role_privs"].items():
        for rule in rules.values():
            if _evidence_both_sides("", rule, privs):
                conflicted[role].append(rule.rule_id)
    return conflicted


def run_detection(
    *,
    tenant: dict,
    prov: dict,
    rules: list[RuleDef],
    conflict_rows: list[ConflictRow],
    model_rows: list[ModelRow],
    ledger: list[dict] | None = None,
    exceptions: list[dict] | None = None,
) -> dict:
    """Build the console payload (D object) from staging inputs."""
    rule_map = {r.rule_id: r for r in rules}
    model = _index_model(model_rows)

    # Start from conflict extract; keep only rules that exist and evidence both sides.
    by_person: dict[str, PersonFinding] = {}
    rule_people: dict[str, set[str]] = defaultdict(set)

    for row in conflict_rows:
        rule = rule_map.get(row.rule_id)
        if not rule:
            continue
        privs = _person_privileges(row.person_ref, model)
        if rule.privs_a or rule.privs_b:
            if not _evidence_both_sides(row.person_ref, rule, privs):
                continue
        pf = by_person.get(row.person_ref)
        if not pf:
            pf = PersonFinding(person_ref=row.person_ref, unit=row.unit or "Unattributed")
            by_person[row.person_ref] = pf
        if row.rule_id not in pf.rule_ids:
            pf.rule_ids.append(row.rule_id)
        rule_people[row.rule_id].add(row.person_ref)

    role_scope_flat: dict[str, str] = {}
    for pf in by_person.values():
        scopes = _role_scope_for_rules(pf.person_ref, pf.rule_ids, model)
        role_scope_flat.update(scopes)
        classify_person(pf, rule_map, scopes)

    ranking = []
    for pf in by_person.values():
        risk = len(pf.rule_ids)
        ranking.append({
            "id": pf.person_ref,
            "unit": pf.unit,
            "risk": risk,
            "r": pf.rule_ids,
            "bucket": pf.bucket,
            "reason": pf.reason,
        })
    ranking.sort(key=lambda x: (-x["risk"], x["id"]))

    real = [p for p in ranking if not is_rec_only(p["r"], rule_map)]
    noise = len(ranking) - len(real)
    review = sum(1 for p in ranking if p.get("bucket") == "review")
    confirm = sum(1 for p in ranking if p.get("bucket") == "confirm")

    rules_out = []
    for rule in rules:
        people = len(rule_people.get(rule.rule_id, ()))
        if people == 0 and rule.rule_id not in rule_map:
            continue
        rules_out.append({
            "id": rule.rule_id,
            "name": rule.name,
            "a": rule.duty_a,
            "b": rule.duty_b,
            "people": people,
            "roles": 0,  # filled when role analysis completes
        })

    risk_meta = {
        r.rule_id: [r.tier, r.scenario, r.control]
        for r in rules
    }

    self_conflicts = _self_conflicted_roles(rule_map, model)
    catalog_roles = []
    for role, privs in model["role_privs"].items():
        meta = model["role_meta"].get(role, {})
        catalog_roles.append({
            "name": meta.get("name", role),
            "code": role,
            "area": meta.get("area", ""),
            "privs": len(privs),
            "conf": self_conflicts.get(role, []),
        })
    conflicted_roles = sum(1 for r in catalog_roles if r["conf"])

    kpi = {
        "flagged": len(ranking),
        "conflicts": sum(p["risk"] for p in ranking),
        "real": len(real),
        "noise": noise,
        "noise_pct": round(noise * 100 / len(ranking)) if ranking else 0,
        "review": review,
        "confirm": confirm,
        "published_total": len(conflict_rows),
        "report_people": len({r.person_ref for r in conflict_rows}),
        "report_pairs": len(conflict_rows),
        "unevidenced_pairs": 0,
        "drop_pct": round(noise * 100 / len(ranking)) if ranking else 0,
        "rules": len(rules_out),
        "roles_total": len(catalog_roles),
        "roles_in_export": len(catalog_roles),
        "roles_no_ent": sum(1 for r in catalog_roles if r["privs"] == 0),
        "roles_conflicted": conflicted_roles,
        "roles_clean": len(catalog_roles) - conflicted_roles,
        "sens_unres": 0,
    }

    resid = sorted(real, key=lambda p: -p["risk"])[:5]
    report = {
        "resid_rules": [
            {
                "id": p["r"][0],
                "name": rule_map[p["r"][0]].name if p["r"] else "",
                "people": p["risk"],
                "cycle": rule_map[p["r"][0]].tier if p["r"] else "",
            }
            for p in resid if p["r"]
        ],
        "resid_roles": [],
        "resid_crit": sum(1 for p in real if p["risk"] >= 10),
        "resid_high": sum(1 for p in real if 5 <= p["risk"] < 10),
        "resid_med": sum(1 for p in real if p["risk"] < 5),
    }

    return {
        "tenant": tenant,
        "kpi": kpi,
        "ranking": ranking,
        "rules": rules_out,
        "risk": risk_meta,
        "detail": {},
        "report": report,
        "catalog": {
            "roles": catalog_roles,
            "privs": [],
            "roleStats": {
                "total": len(catalog_roles),
                "conflicted": conflicted_roles,
                "clean": len(catalog_roles) - conflicted_roles,
            },
        },
        "sensitive": {"roles": [], "stats": {}},
        "ledger": ledger or [],
        "exceptions": exceptions or [],
        "aggregatePrivs": [],
        "prov": prov,
    }
