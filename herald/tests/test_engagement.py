"""Engagement classification and iteria capability baseline tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ORACLE_PASSWORD", "x")
os.environ.setdefault("ORACLE_DSN", "x")
os.environ.setdefault("GENAI_REGION", "us-chicago-1")
os.environ.setdefault("GENAI_MODEL_OCID", "ocid1.generativeaimodel.oc1.test.aaaa")
os.environ.setdefault("GENAI_COMPARTMENT_ID", "ocid1.compartment.oc1..test")
os.environ.setdefault("HARALD_SESSION_SECRET", "unit-test-secret-value")

from app import engagement, iteria_capabilities, site_grounding


def test_classify_ai_enablement_rfp():
    text = (
        "Enterprise AI Adoption and Enablement for TTUHSC. "
        "Governance, training, generative AI, HIPAA-aware workflows."
    )
    profile = engagement.classify_text(text)
    assert profile.kind == "ai_enablement"
    assert profile.label


def test_classify_erp_modernization():
    text = "Oracle Cloud Fusion ERP financials HCM payroll procurement implementation"
    profile = engagement.classify_text(text)
    assert profile.kind == "erp_modernization"


def test_ai_section_plan_not_erp_modules():
    profile = engagement.classify_text("AI enablement governance training")
    plan = iteria_capabilities.section_plan(profile)
    titles = [t for t, _ in plan]
    assert "Enterprise AI Strategy and Roadmap" in titles
    assert "Financial Management" not in titles


def test_default_modules_ai_vs_erp():
    ai = engagement.classify_text("AI enablement generative AI governance")
    erp = engagement.classify_text("Oracle Fusion ERP financials HCM payroll")
    assert engagement.default_modules(ai) == ["TECH", "GENERAL"]
    assert "FIN" in engagement.default_modules(erp)


def test_site_queries_ai_profile():
    profile = engagement.classify_text("AI enablement governance")
    qs = site_grounding.build_queries("staff training plan", "TECH", profile)
    joined = " ".join(qs).lower()
    assert "iteria.us" in joined
    assert "ai" in joined


def test_capability_context_always_present():
    profile = engagement.classify_text("consulting services")
    block = iteria_capabilities.context_for(profile)
    assert "ITERIA CAPABILITY BASELINE" in block
    assert "AI adoption" in block
