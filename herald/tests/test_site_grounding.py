"""Unit tests for site grounding helpers (no network)."""
from app import site_grounding
from app.config import cfg


def test_allowed_url_oracle_and_iteria():
    assert site_grounding._allowed_url("https://docs.oracle.com/en/cloud/saas/financials/")
    assert site_grounding._allowed_url("https://www.iteria.us/services")
    assert not site_grounding._allowed_url("https://example.com/oracle")


def test_build_queries_include_product_and_sites():
    qs = site_grounding.build_queries("role based access control", "TECH")
    assert qs
    joined = " ".join(qs).lower()
    assert "oracle" in joined
    assert "site:docs.oracle.com" in joined or "docs.oracle.com" in joined


def test_format_context_empty():
    assert site_grounding.format_context([]) == ""


def test_format_context_includes_snippets():
    block = site_grounding.format_context([{
        "title": "Fusion Security",
        "url": "https://docs.oracle.com/security",
        "snippet": "Role-based access control is configured in Security Console.",
    }])
    assert "SITE" in block or "ORACLE" in block
    assert "Security Console" in block
    assert "docs.oracle.com" in block


def test_clean_text_strips_tags():
    assert "<" not in site_grounding._clean_text("<p>Hello <b>world</b></p>")


def test_configured_shape():
    info = site_grounding.configured()
    assert "enabled" in info
    assert "domains" in info
    assert isinstance(info["domains"], list)
    assert cfg.site_grounding_enabled in (True, False)
