"""Regression tests for agency formatting profiles.

All four proposals came out of HARALD formatted identically, and the reason was
one wrong key. ``proposal_docx.build`` looked up ``meta["client_name"]`` to pick
the profile; ``studio.export_docx`` sets ``meta["client"]``. The lookup found an
empty string every time, matched nothing, and every response fell through to
DEFAULT_PROFILE - no section breaks for the agencies that require tab dividers,
no gutter for the ones submitted in binders, no mirrored margins for Nashua's
double-sided requirement.

Verified against the live application before the fix: all four exports had
gutter=0, no mirrorMargins, and two page breaks (title and contents only).
"""

import io
import zipfile

import pytest

from app import proposal_docx as P


DRAFT = "\n".join([
    "# Cover",
    "",
    "Body text for the cover section.",
    "",
    "# 1.0 Executive Summary",
    "",
    "Body text.",
    "",
    "## 1.1 Detail",
    "",
    "More body text.",
    "",
    "# 2.0 Company Background",
    "",
    "Background text.",
    "",
    "# 3.0 Approach",
    "",
    "Still more body text.",
])


def _meta(client, solicitation=None):
    return {"title": client, "subtitle": "Proposal", "client": client,
            "client_name": client, "solicitation": solicitation,
            "firm": "iteria", "footer": client}


def _parts(document):
    buffer = io.BytesIO()
    document.save(buffer)
    buffer.seek(0)
    archive = zipfile.ZipFile(buffer)
    return {
        "document": archive.read("word/document.xml").decode("utf-8"),
        "settings": archive.read("word/settings.xml").decode("utf-8"),
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client,expected", [
    ("City of Nashua", "nashua"),
    ("Town of Salem, New Hampshire", "salem"),
    ("Jefferson County Sheriff's Office", "jefferson"),
    ("Texas Tech University Health Sciences Center", "ttuhsc"),
])
def test_every_live_client_name_resolves(client, expected):
    name, _ = P.resolve_profile(client)
    assert name == expected


def test_texas_tech_does_not_contain_the_string_ttuhsc():
    """The bug this pins: substring-matching the profile key against the client
    name can never match, because nobody writes the acronym in the name."""
    assert "ttuhsc" not in "Texas Tech University Health Sciences Center".lower()
    assert P.resolve_profile("Texas Tech University Health Sciences Center")[0] == "ttuhsc"


@pytest.mark.parametrize("solicitation,expected", [
    ("RFP 0619-093026", "nashua"),
    ("RFP 2026-008", "salem"),
    ("RFP 739-SL3821039", "ttuhsc"),
])
def test_the_solicitation_number_alone_is_enough(solicitation, expected):
    assert P.resolve_profile(None, solicitation)[0] == expected


def test_an_unknown_agency_falls_back_to_the_house_style():
    name, profile = P.resolve_profile("Borough of Nowhere", "RFP 1")
    assert name == "default"
    assert profile == P.DEFAULT_PROFILE


def test_resolution_ignores_empty_hints():
    assert P.resolve_profile(None, "", None, "City of Nashua")[0] == "nashua"


# ---------------------------------------------------------------------------
# The rules actually reach the document
# ---------------------------------------------------------------------------

def test_nashua_is_built_for_double_sided_printing():
    """Section V requires responses printed double-sided. That needs mirrored
    margins and a gutter, or text creeps into the binding on alternate pages."""
    parts = _parts(P.build(DRAFT, _meta("City of Nashua")))
    assert "mirrorMargins" in parts["settings"]
    assert "evenAndOddHeaders" in parts["settings"]
    assert 'w:gutter="720"' in parts["document"]      # half an inch


@pytest.mark.parametrize("client", [
    "Town of Salem, New Hampshire",
    "Texas Tech University Health Sciences Center",
])
def test_single_sided_agencies_get_no_mirrored_margins(client):
    assert "mirrorMargins" not in _parts(P.build(DRAFT, _meta(client)))["settings"]


def test_jefferson_gets_a_binding_gutter_without_mirroring():
    """Section 4.1 wants hard copies in three-ring binders, single sided."""
    parts = _parts(P.build(DRAFT, _meta("Jefferson County Sheriff's Office")))
    assert 'w:gutter="720"' in parts["document"]
    assert "mirrorMargins" not in parts["settings"]


def test_salem_needs_no_gutter():
    parts = _parts(P.build(DRAFT, _meta("Town of Salem, New Hampshire")))
    assert 'w:gutter="720"' not in parts["document"]


@pytest.mark.parametrize("client", [
    "City of Nashua",
    "Town of Salem, New Hampshire",
    "Jefferson County Sheriff's Office",
])
def test_tab_separated_agencies_start_each_section_on_a_new_page(client):
    body = _parts(P.build(DRAFT, _meta(client)))["document"]
    # title page and contents contribute two; the sections add the rest
    assert body.count('w:type="page"') > 2, (
        "an agency that requires tab dividers cannot have sections running "
        "together mid-page"
    )


def test_ttuhsc_is_continuous():
    """TechBid takes one electronic document. There is no binder to divide."""
    body = _parts(P.build(DRAFT, _meta("Texas Tech University Health Sciences Center")))["document"]
    assert body.count('w:type="page"') <= 2


def test_two_agencies_do_not_produce_the_same_document():
    """The whole complaint in one assertion."""
    nashua = _parts(P.build(DRAFT, _meta("City of Nashua")))
    ttuhsc = _parts(P.build(DRAFT, _meta("Texas Tech University Health Sciences Center")))
    assert nashua["settings"] != ttuhsc["settings"]
    assert nashua["document"] != ttuhsc["document"]


# ---------------------------------------------------------------------------
# The key that was wrong
# ---------------------------------------------------------------------------

def test_build_reads_the_key_studio_actually_sets():
    """studio.export_docx sets "client". Reading "client_name" alone is how
    every proposal ended up on the default profile."""
    meta = {"title": "City of Nashua", "client": "City of Nashua",
            "solicitation": "RFP 0619-093026", "firm": "iteria"}
    assert "client_name" not in meta
    assert 'w:gutter="720"' in _parts(P.build(DRAFT, meta))["document"]


def test_studio_passes_the_client_name_through():
    import inspect
    from app import studio
    src = inspect.getsource(studio.export_docx)
    assert '"client_name": client' in src


def test_the_firm_is_named_iteria():
    """It is "iteria". "iteria.us" is the domain."""
    import inspect
    from app import studio
    src = inspect.getsource(studio.export_docx)
    assert '"firm": "iteria"' in src
    assert '"firm": "iteria.us, Inc."' not in src


def test_an_explicit_profile_still_wins():
    parts = _parts(P.build(DRAFT, _meta("City of Nashua"), profile="ttuhsc"))
    assert "mirrorMargins" not in parts["settings"]


def test_an_explicit_profile_dict_is_honoured():
    custom = dict(P.DEFAULT_PROFILE, gutter_in=1.0)
    body = _parts(P.build(DRAFT, _meta("Town of Salem, New Hampshire"), profile=custom))["document"]
    assert 'w:gutter="1440"' in body
