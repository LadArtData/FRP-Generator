#!/usr/bin/env python3
"""Load an active ruleset into warden_rules for a tenant.

The rules file is iteria IP or customer-specific configuration — never commit
real tenant rules to git. Use a local JSON file.

Format:
{
  "rules": [
    {
      "rule_id": "REC-01",
      "name": "Open requisition and manage candidates",
      "cycle": "Hire-to-Retire",
      "module": "Recruiting",
      "tier": "moderate",
      "status": "active",
      "config": {
        "duty_a": "Create requisition",
        "duty_b": "Manage candidates",
        "privs_a": ["PRIV_REQ_CREATE"],
        "privs_b": ["PRIV_CAND_MANAGE"],
        "scenario": "...",
        "control": "..."
      }
    }
  ]
}

Usage:
  python tools/load_rules.py --tenant-id 1 rules.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Load warden_rules from JSON")
    p.add_argument("rules_file", help="Path to rules JSON")
    p.add_argument("--tenant-id", type=int, required=True)
    args = p.parse_args(argv)

    doc = json.loads(Path(args.rules_file).read_text(encoding="utf-8"))
    rules = doc.get("rules") or doc
    if not isinstance(rules, list):
        print("rules file must contain a 'rules' array", file=sys.stderr)
        return 1

    db.init_pool()
    for rule in rules:
        if "rule_id" not in rule:
            print("each rule requires rule_id", file=sys.stderr)
            return 1
        db.upsert_rule(args.tenant_id, rule)

    print(f"Loaded {len(rules)} rules for tenant {args.tenant_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
