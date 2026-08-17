"""Pricing matrix helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ORACLE_PASSWORD", "x")
os.environ.setdefault("ORACLE_DSN", "x")
os.environ.setdefault("GENAI_REGION", "us-chicago-1")
os.environ.setdefault("GENAI_MODEL_OCID", "ocid1.generativeaimodel.oc1.test.aaaa")
os.environ.setdefault("GENAI_COMPARTMENT_ID", "ocid1.compartment.oc1..test")
os.environ.setdefault("HARALD_SESSION_SECRET", "unit-test-secret-value")

from unittest.mock import patch

from app import pricing_matrix


def test_ai_enablement_default_lines():
    with patch("app.pricing_matrix.opportunities.get") as get, patch(
        "app.pricing_matrix.opportunities.grounding_context"
    ) as gc:
        get.return_value = {"client_name": "TTUHSC", "extracted_json": "null"}
        gc.return_value = (
            "Enterprise AI adoption governance training HIPAA TTUHSC",
            {"pain_points": "AI adoption and enablement"},
        )
        lines = pricing_matrix.default_lines_for(1)
    items = [line["line_item"] for line in lines]
    assert any("AI readiness" in x for x in items)
    assert not any("Financials configuration" in x for x in items)
