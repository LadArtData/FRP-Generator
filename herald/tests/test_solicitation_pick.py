"""Picking the real solicitation out of an attachment set.

Written against two live mis-parses:

  * City of Nashua — six attachments named 5259-5264. HAROLD parsed
    "5263 (1).xlsx", a NH Dept of Education Title I comparability workbook, and
    reported the client as "New Hampshire Department of Education". The actual
    solicitation is "5259 - YS markups (1).pdf", a 63-page ERP RFP.
  * Jefferson County Sheriff's Office — five attachments. HAROLD parsed
    "Attachment C2.docx" and returned client_name "County" with every other
    field empty. The solicitation is "RFP Specifications.pdf".
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.studio import _pick_solicitation, _solicitation_rank  # noqa: E402


def _docs(*specs):
    """specs: (doc_id, filename) or (doc_id, filename, role)."""
    return [{"doc_id": s[0], "filename": s[1],
             "doc_role": s[2] if len(s) > 2 else "rfp"} for s in specs]


# --- ranking ----------------------------------------------------------------

def test_specification_pdf_outranks_lettered_attachments():
    spec = _solicitation_rank("Jefferson County Sheriffs Office RFP Specifications.pdf", 40_000)
    c2 = _solicitation_rank("Jefferson County Sheriffs Office RFP - Attachment C2.docx", 8_000)
    b = _solicitation_rank("Jefferson County Sheriffs Office RFP - Attachment B.xlsx", 30_000)
    assert spec > c2
    assert spec > b


def test_a_big_spreadsheet_does_not_beat_a_modest_specification():
    """Length is a tie-breaker, not the driver — a 24-tab requirements workbook
    is longer than the RFP and must still lose."""
    workbook = _solicitation_rank("5260 (1).xlsx", 250_000)
    rfp = _solicitation_rank("5259 - YS markups (1).pdf", 22_000)
    assert rfp > workbook


def test_certification_and_calculator_files_rank_last():
    """The Nashua set included DOE-25, an MOE calculator and a Title I
    certification. None of them is the solicitation."""
    rfp = _solicitation_rank("RFP Specifications.pdf", 30_000)
    for junk in ("Title I Certification.xlsx", "NSD MOE Calculator.xlsx",
                 "Lead Quarterly SF425 form.pdf", "Appendix B Pricing Format.xlsx",
                 "Appendix C Client References.docx"):
        assert _solicitation_rank(junk, 20_000) < rfp, junk


def test_spreadsheets_are_penalised_against_documents():
    as_pdf = _solicitation_rank("solicitation.pdf", 10_000)
    as_xlsx = _solicitation_rank("solicitation.xlsx", 10_000)
    assert as_xlsx < as_pdf
    assert as_pdf > 0


# --- selection --------------------------------------------------------------

def test_jefferson_county_picks_the_specification_not_attachment_c2(monkeypatch):
    import app.studio as studio
    lengths = {1: 40_000, 2: 6_000, 3: 30_000, 4: 25_000, 5: 8_000}
    monkeypatch.setattr(studio.documents, "get_text", lambda d: "x" * lengths[d])
    opp = {"documents": _docs(
        (1, "Jefferson County Sheriffs Office RFP Specifications.pdf"),
        (2, "Jefferson County Sheriffs Office RFP - Attachment A.docx"),
        (3, "Jefferson County Sheriffs Office RFP - Attachment B.xlsx"),
        (4, "Jefferson County Sheriffs Office RFP - Attachment C1.xlsx"),
        (5, "Jefferson County Sheriffs Office RFP - Attachment C2.docx"),
    )}
    picked, reason = _pick_solicitation(opp, requested_doc_id=5)
    assert picked == 1, f"picked {picked}: {reason}"


def test_nashua_picks_the_rfp_not_the_title_i_workbook(monkeypatch):
    import app.studio as studio
    lengths = {59: 22_000, 60: 250_000, 61: 90_000, 62: 90_000, 63: 40_000, 64: 3_000}
    monkeypatch.setattr(studio.documents, "get_text", lambda d: "x" * lengths[d])
    opp = {"documents": _docs(
        (59, "5259 - YS markups (1).pdf"),
        (60, "5260 (1).xlsx"),
        (61, "5261 (1).xlsx"),
        (62, "5262 (1).xlsx"),
        (63, "5263 (1).xlsx"),
        (64, "5264 (1).pdf"),
    )}
    picked, _ = _pick_solicitation(opp, requested_doc_id=63)
    # Opaque numeric names carry no keyword signal, so this falls to format and
    # length: a PDF of real size must beat the spreadsheets.
    assert picked in (59, 64)
    assert picked == 59, "the 22k-word PDF is the solicitation; 5264 is a 2-page form"


def test_single_attachment_is_returned_untouched():
    opp = {"documents": _docs((7, "Whatever.pdf"))}
    assert _pick_solicitation(opp, requested_doc_id=7) == (7, "only attachment")


def test_non_rfp_attachments_are_never_candidates(monkeypatch):
    import app.studio as studio
    monkeypatch.setattr(studio.documents, "get_text", lambda d: "x" * 50_000)
    opp = {"documents": _docs(
        (1, "RFP Specifications.pdf"),
        (2, "iteria past proposal.docx", "reference"),
    )}
    assert _pick_solicitation(opp, requested_doc_id=1)[0] == 1


def test_a_close_human_choice_is_not_overridden(monkeypatch):
    """If someone deliberately attached a specific document, a marginal score
    difference must not silently discard that choice."""
    import app.studio as studio
    monkeypatch.setattr(studio.documents, "get_text", lambda d: "x" * 30_000)
    opp = {"documents": _docs((1, "RFP Specifications.pdf"),
                              (2, "RFP Solicitation.pdf"))}
    picked, reason = _pick_solicitation(opp, requested_doc_id=2)
    assert picked == 2
    assert "competitively" in reason


def test_unreadable_document_does_not_break_selection(monkeypatch):
    import app.studio as studio

    def boom(doc_id):
        if doc_id == 2:
            raise RuntimeError("no extractable text")
        return "x" * 40_000

    monkeypatch.setattr(studio.documents, "get_text", boom)
    opp = {"documents": _docs((1, "RFP Specifications.pdf"),
                              (2, "Scanned Attachment.pdf"))}
    assert _pick_solicitation(opp, requested_doc_id=2)[0] == 1
