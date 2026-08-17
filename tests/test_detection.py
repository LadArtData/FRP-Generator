"""Offline tests for detection and triage — no database, no tenant data in repo."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.detection.engine import run_detection
from app.detection.models import ConflictRow, ModelRow, RuleDef
from app.detection.triage import is_rec_only, severity_label


RULES = [
    RuleDef(
        "REC-01", "Open requisition and manage candidates",
        "Create requisition", "Manage candidates",
        tier="moderate",
        privs_a={"PRIV_REQ_CREATE"}, privs_b={"PRIV_CAND_MANAGE"},
    ),
    RuleDef(
        "PAY-01", "Maintain payroll and approve payroll",
        "Maintain payroll", "Approve payroll",
        tier="critical",
        privs_a={"PRIV_PAY_MAINT"}, privs_b={"PRIV_PAY_APPROVE"},
    ),
]


def _model():
    return [
        ModelRow("ROLE", "ROLE_HM", attrs={"name": "Hiring Manager", "scope": "department"}),
        ModelRow("ROLE", "ROLE_PAY", attrs={"name": "Payroll Admin", "scope": "unrestricted"}),
        ModelRow("GRANT", "PRIV_REQ_CREATE", parent_key="ROLE_HM"),
        ModelRow("GRANT", "PRIV_CAND_MANAGE", parent_key="ROLE_HM"),
        ModelRow("GRANT", "PRIV_PAY_MAINT", parent_key="ROLE_PAY"),
        ModelRow("GRANT", "PRIV_PAY_APPROVE", parent_key="ROLE_PAY"),
        ModelRow("HOLDER", "ROLE_HM", parent_key="P001"),
        ModelRow("HOLDER", "ROLE_PAY", parent_key="P002"),
    ]


class TestTriage:
    def test_recruiting_only_is_noise(self):
        assert is_rec_only(["REC-01"], {r.rule_id: r for r in RULES})

    def test_severity_bands(self):
        assert severity_label(10) == "critical"
        assert severity_label(5) == "high"
        assert severity_label(2) == "medium"


class TestDetection:
    def test_flags_both_sides_evidenced(self):
        conflicts = [
            ConflictRow("P001", "REC-01", unit="Unit A"),
            ConflictRow("P002", "PAY-01", unit="Unit B"),
        ]
        payload = run_detection(
            tenant={"name": "Test Org", "env": "Test"},
            prov={"conflict": {"pretty": "2026-01-01", "source": "test"},
                  "model": {"pretty": "2026-01-15", "source": "test", "scope": "HCM", "roles": 2, "grants": 4},
                  "drift_days": 14},
            rules=RULES,
            conflict_rows=conflicts,
            model_rows=_model(),
        )
        assert payload["kpi"]["flagged"] == 2
        assert payload["kpi"]["real"] == 1
        assert payload["kpi"]["noise"] == 1
        assert {p["id"] for p in payload["ranking"] if p["id"] not in {x["id"] for x in payload["ranking"] if is_rec_only(x["r"], {r.rule_id: r for r in RULES})}} == {"P002"}

    def test_drops_unevidenced_conflict(self):
        conflicts = [ConflictRow("P001", "PAY-01", unit="Unit A")]
        payload = run_detection(
            tenant={"name": "T", "env": "T"},
            prov={"conflict": {}, "model": {}, "drift_days": 0},
            rules=RULES,
            conflict_rows=conflicts,
            model_rows=[ModelRow("HOLDER", "ROLE_HM", parent_key="P001")],
        )
        assert payload["kpi"]["flagged"] == 0
