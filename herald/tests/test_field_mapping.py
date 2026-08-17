"""Regression tests for the parsed_fields -> studio_form translation.

These are written against the TTUHSC incident: RFP 739-SL3821039 parsed
correctly and then reached the form as "County Government / Full implementation
/ Cloud-first cutover." with the due date silently dropped, because nothing
mapped one vocabulary onto the other.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import field_mapping as fm  # noqa: E402

TTUHSC_PARSED = {
    "client_name": "Texas Tech University Health Sciences Center",
    "agency": "TTUHSC",
    "industry": "higher education, healthcare",
    "primary_contact": "Shawn Olbeter",
    "annual_budget": "",
    "legacy_systems": "",
    "rfp_number": "739-SL3821039",
    "due_date": "September 21, 2026",
    "pain_points": "AI adoption and enablement",
    "required_modules": [],
    "engagement_type": "ai_enablement",
}

# The state the live bid was actually in before autofill ran.
STALE_FORM = {
    "client_name": "Texas Tech University Health Sciences Center",
    "industry": "County Government",
    "primary_contact": "Shawn Olbeter",
    "annual_budget": "",
    "legacy_systems": "",
    "rfp_number": "739-SL3821039",
    "due_date": "",
    "engagement_type": "Full implementation",
    "primary_competition": "",
    "win_theme": "Cloud-first cutover.",
    "project_manager": "",
    "solution_architect": "",
    "pain_points": [],
    "proposed_modules": [],
}


# --- due date ---------------------------------------------------------------

def test_due_date_written_as_iso_for_date_input():
    # input[type=date] discards anything non-ISO, which is how a deadline
    # vanished from a bid that had one.
    assert fm.map_due_date("September 21, 2026") == "2026-09-21"


def test_due_date_accepts_the_spellings_agencies_actually_use():
    for value in ("Sept 21 2026", "09/21/2026", "2026-09-21",
                  "21 September 2026", "September 21st, 2026",
                  "September 21, 2026 at 3:00 PM CT"):
        assert fm.map_due_date(value) == "2026-09-21", value


def test_unparseable_due_date_is_empty_not_garbage():
    assert fm.map_due_date("upon request") == ""
    assert fm.map_due_date(None) == ""


# --- industry ---------------------------------------------------------------

def test_health_sciences_centre_is_not_a_county_government():
    assert fm.map_industry("higher education, healthcare") == \
        "Healthcare / Health Sciences"


def test_industry_falls_back_to_client_name_when_parse_is_silent():
    assert fm.map_industry("", fallback_text="Jefferson County Sheriff's Office") == \
        "County Government"
    assert fm.map_industry("", fallback_text="Town of Salem") == "City / Municipality"


def test_industry_maps_only_onto_real_form_options():
    for source in ("higher education, healthcare", "county", "town of x", "state of y"):
        result = fm.map_industry(source)
        assert result in fm.INDUSTRY_OPTIONS, result


# --- engagement type --------------------------------------------------------

def test_ai_enablement_code_maps_to_the_ai_option():
    assert fm.map_engagement_type("ai_enablement") == "AI enablement & consulting"


def test_erp_code_maps_to_full_implementation():
    assert fm.map_engagement_type("erp_modernization") == "Full implementation"


def test_engagement_type_never_invents_an_option():
    assert fm.map_engagement_type("") == ""
    assert fm.map_engagement_type("something unrelated") == ""


# --- pain points ------------------------------------------------------------

def test_free_text_pain_point_survives_the_closed_checkbox_vocabulary():
    matched, text = fm.map_pain_points("AI adoption and enablement")
    # No checkbox can express it...
    assert matched == []
    # ...so the text has to carry it, or the client's actual need is lost.
    assert text == "AI adoption and enablement"


def test_recognisable_pain_points_still_tick_their_boxes():
    matched, _ = fm.map_pain_points("manual processes and an aging system")
    assert matched == ["Manual processes", "Aging system"]


# --- whole-record reconcile -------------------------------------------------

def test_reconcile_corrects_every_field_the_incident_got_wrong():
    form, changed, _ = fm.reconcile(STALE_FORM, TTUHSC_PARSED)
    assert form["industry"] == "Healthcare / Health Sciences"
    assert form["engagement_type"] == "AI enablement & consulting"
    assert form["due_date"] == "2026-09-21"
    assert form["pain_points_text"] == "AI adoption and enablement"
    assert set(changed) >= {"industry", "engagement_type", "due_date"}


def test_reconcile_reports_a_stale_win_theme_rather_than_shipping_it():
    _, _, conflicts = fm.reconcile(STALE_FORM, TTUHSC_PARSED)
    assert [c["field"] for c in conflicts] == ["win_theme"]
    assert "Cloud-first cutover." in conflicts[0]["value"]


def test_reconcile_never_overwrites_human_authored_fields():
    existing = dict(STALE_FORM, project_manager="A. Rivera",
                    solution_architect="J. Okafor", primary_competition="Deloitte")
    form, _, _ = fm.reconcile(existing, TTUHSC_PARSED)
    assert form["project_manager"] == "A. Rivera"
    assert form["solution_architect"] == "J. Okafor"
    assert form["primary_competition"] == "Deloitte"
    assert form["win_theme"] == "Cloud-first cutover."


def test_a_silent_solicitation_does_not_blank_existing_values():
    existing = dict(STALE_FORM, annual_budget="$412M operating budget")
    form, _, _ = fm.reconcile(existing, TTUHSC_PARSED)  # parsed budget is ""
    assert form["annual_budget"] == "$412M operating budget"


def test_reconcile_on_an_empty_form_is_just_the_mapping():
    form, changed, conflicts = fm.reconcile({}, TTUHSC_PARSED)
    assert form["industry"] == "Healthcare / Health Sciences"
    assert form["due_date"] == "2026-09-21"
    assert conflicts == []
    assert changed["industry"]["from"] is None


def test_modules_map_onto_form_labels():
    assert fm.map_modules(["financials", "HR", "purchasing"]) == \
        ["Financials", "HCM", "Procurement"]
    assert fm.map_modules([]) == []
