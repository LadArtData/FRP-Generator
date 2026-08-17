"""Tests for rule loading from warden_rules.config_json."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rules import rule_from_row, rule_to_config_json


def test_rule_from_row():
    cfg = {
        "duty_a": "Maintain payroll",
        "duty_b": "Approve payroll",
        "privs_a": ["PAY_MAINT"],
        "privs_b": ["PAY_APPR"],
        "scenario": "Single person can change and approve pay.",
        "control": "Dual control on payroll",
    }
    row = ("PAY-01", "Payroll maintain + approve", "critical", json.dumps(cfg))
    rule = rule_from_row(row)
    assert rule.rule_id == "PAY-01"
    assert rule.tier == "critical"
    assert "PAY_MAINT" in rule.privs_a
    assert rule.scenario.startswith("Single person")


def test_rule_to_config_json_roundtrip():
    doc = {
        "rule_id": "REC-01",
        "name": "Recruiting",
        "config": {
            "duty_a": "A",
            "duty_b": "B",
            "privs_a": ["X"],
            "privs_b": ["Y"],
        },
    }
    raw = rule_to_config_json(doc)
    rule = rule_from_row(("REC-01", "Recruiting", "moderate", raw))
    assert rule.duty_a == "A"
    assert rule.privs_b == {"Y"}
