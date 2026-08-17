"""Load active SOD rules from warden_rules.config_json."""

from __future__ import annotations

import json
import logging
from typing import Any

from .detection.models import RuleDef

log = logging.getLogger("warden.rules")


def parse_config_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("invalid config_json on rule row")
        return {}


def rule_from_row(row: tuple) -> RuleDef:
    """Map a warden_rules query row to RuleDef.

    Expected columns: rule_id, name, tier, config_json
    """
    rule_id, name, tier, config_raw = row
    cfg = parse_config_json(config_raw)
    privs_a = set(cfg.get("privs_a") or [])
    privs_b = set(cfg.get("privs_b") or [])
    return RuleDef(
        rule_id=rule_id,
        name=name or rule_id,
        duty_a=cfg.get("duty_a") or "",
        duty_b=cfg.get("duty_b") or "",
        tier=(tier or "moderate").lower(),
        scenario=cfg.get("scenario") or "",
        control=cfg.get("control") or "",
        privs_a=privs_a,
        privs_b=privs_b,
    )


def rule_to_config_json(rule: dict[str, Any]) -> str:
    """Serialize a rule definition document for warden_rules.config_json."""
    cfg = rule.get("config") or rule
    payload = {
        "duty_a": cfg.get("duty_a", ""),
        "duty_b": cfg.get("duty_b", ""),
        "privs_a": cfg.get("privs_a") or [],
        "privs_b": cfg.get("privs_b") or [],
        "scenario": cfg.get("scenario", ""),
        "control": cfg.get("control", ""),
    }
    return json.dumps(payload)
