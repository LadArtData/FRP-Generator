"""Detection domain models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuleDef:
    rule_id: str
    name: str
    duty_a: str
    duty_b: str
    tier: str = "moderate"
    scenario: str = ""
    control: str = ""
    privs_a: set[str] = field(default_factory=set)
    privs_b: set[str] = field(default_factory=set)


@dataclass
class ConflictRow:
    person_ref: str
    rule_id: str
    unit: str = "Unattributed"
    disposition: str = ""


@dataclass
class ModelRow:
    entity_type: str  # HOLDER|ROLE|GRANT|PRIV
    entity_key: str
    parent_key: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass
class PersonFinding:
    person_ref: str
    unit: str
    rule_ids: list[str] = field(default_factory=list)
    bucket: str = ""
    reason: str = ""
