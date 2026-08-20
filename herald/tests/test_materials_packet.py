"""Regression tests for the submission packet.

Three faults were found by unzipping the four real packets rather than by
trusting the manifest:

1. Jefferson County's packet carried our won Outagamie County proposal in
   03_attachments, because that document is attached to the opportunity so the
   drafter can retrieve from it.
2. Nashua's packet carried the same agency workbook twice - once with all
   3,041 rows answered and once with the 45 the old detector found.
3. Nothing in the packet said what a human still had to do, so a packet that
   downloads clean is indistinguishable from one that is ready to submit.
"""

import pytest

from app import studio


# ---------------------------------------------------------------------------
# 1. Internal material must not ride out to the agency.
# ---------------------------------------------------------------------------

def test_library_document_is_internal():
    doc = {"doc_id": 7, "filename": "iteria.us Technical Proposal.docx",
           "doc_role": "rfp", "promoted_to_lib": "Y"}
    assert studio._is_internal_doc(doc) is True


def test_agency_attachment_is_not_internal():
    doc = {"doc_id": 8, "filename": "Attachment B.xlsx",
           "doc_role": "questionnaire", "promoted_to_lib": "N"}
    assert studio._is_internal_doc(doc) is False


@pytest.mark.parametrize("role", ["reference", "exemplar", "library", "sample",
                                  "REFERENCE", " Exemplar "])
def test_reference_roles_are_internal(role):
    assert studio._is_internal_doc({"doc_role": role}) is True


@pytest.mark.parametrize("flag", ["y", " Y ", "Y"])
def test_promoted_flag_is_read_loosely(flag):
    assert studio._is_internal_doc({"promoted_to_lib": flag}) is True


def test_missing_fields_default_to_shippable():
    """An attachment with no metadata is the agency's until proven otherwise."""
    assert studio._is_internal_doc({}) is False
    assert studio._is_internal_doc({"promoted_to_lib": None,
                                    "doc_role": None}) is False


# ---------------------------------------------------------------------------
# 2. One filled workbook per agency file.
# ---------------------------------------------------------------------------

def _stub_list(monkeypatch, rows):
    monkeypatch.setattr(studio.questionnaires, "list_for_opportunity",
                        lambda opp_id: list(rows))


def test_duplicate_import_of_one_workbook_keeps_the_answered_one(monkeypatch):
    _stub_list(monkeypatch, [
        {"q_id": 41, "source_doc_id": 5260, "answered": 3041, "item_count": 3041},
        {"q_id": 21, "source_doc_id": 5260, "answered": 45, "item_count": 45},
    ])
    kept = studio.packet_questionnaires(61)
    assert [q["q_id"] for q in kept] == [41]


def test_distinct_workbooks_are_all_kept(monkeypatch):
    _stub_list(monkeypatch, [
        {"q_id": 41, "source_doc_id": 5260, "answered": 3041, "item_count": 3041},
        {"q_id": 22, "source_doc_id": 5262, "answered": 96, "item_count": 96},
        {"q_id": 23, "source_doc_id": 5263, "answered": 118, "item_count": 118},
    ])
    assert [q["q_id"] for q in studio.packet_questionnaires(61)] == [22, 23, 41]


def test_later_import_wins_a_tie_on_answered_rows(monkeypatch):
    _stub_list(monkeypatch, [
        {"q_id": 21, "source_doc_id": 5260, "answered": 45, "item_count": 45},
        {"q_id": 41, "source_doc_id": 5260, "answered": 45, "item_count": 45},
    ])
    assert [q["q_id"] for q in studio.packet_questionnaires(61)] == [41]


def test_a_questionnaire_without_a_source_document_is_not_collapsed(monkeypatch):
    """Two rows with a null source doc are two different workbooks, not one."""
    _stub_list(monkeypatch, [
        {"q_id": 5, "source_doc_id": None, "answered": 10, "item_count": 10},
        {"q_id": 6, "source_doc_id": None, "answered": 2, "item_count": 2},
    ])
    assert [q["q_id"] for q in studio.packet_questionnaires(61)] == [5, 6]


def test_list_for_opportunity_exposes_source_doc_id():
    """The dedupe key has to come out of the query, not be guessed."""
    import inspect
    from app import questionnaires
    src = inspect.getsource(questionnaires.list_for_opportunity)
    assert "q.source_doc_id" in src
    assert '"source_doc_id"' in src


# ---------------------------------------------------------------------------
# 3. The packet has to say what is missing.
# ---------------------------------------------------------------------------

def _checklist_text(opp, files=(), open_items=()):
    return studio._checklist(opp, list(files), list(open_items)).decode("utf-8")


def test_pricing_marker_becomes_a_blocker():
    text = _checklist_text(
        {"opp_id": 61, "client_name": "City of Nashua", "status": "evaluating",
         "draft_text": "6.2 Cost\n\nPRICING PENDING\n"})
    assert studio.PRICING_MARKER in text
    assert "delete the marker" in text


def test_no_pricing_marker_no_pricing_blocker():
    text = _checklist_text(
        {"opp_id": 83, "client_name": "TTUHSC", "status": "bidding",
         "draft_text": "6.2 Cost\n\n$412,000 fixed fee.\n"})
    assert "delete the marker" not in text


def test_unanswerable_workbook_is_named_as_an_open_item():
    text = _checklist_text(
        {"opp_id": 61, "client_name": "City of Nashua", "status": "evaluating"},
        open_items=["5263 (1).xlsx - not answered by HARALD: "
                    "No response column is mapped on this workbook"])
    assert "5263 (1).xlsx" in text
    assert "No response column is mapped" in text


def test_checklist_always_asks_for_signatures_people_and_references():
    text = _checklist_text({"opp_id": 24, "client_name": "Salem", "status": "evaluating"})
    assert "Signature" in text
    assert "resumes" in text
    assert "reference" in text.lower()


def test_checklist_warns_that_pricing_folder_is_internal():
    text = _checklist_text({"opp_id": 24, "client_name": "Salem", "status": "evaluating"})
    assert "05_pricing" in text
    assert "Remove it" in text


def test_checklist_lists_the_files_in_the_packet():
    text = _checklist_text(
        {"opp_id": 24, "client_name": "Salem", "status": "evaluating"},
        files=["Salem_Proposal.docx", "Attachment B_iteria_response.xlsx"])
    assert "Salem_Proposal.docx" in text
    assert "Attachment B_iteria_response.xlsx" in text


def test_checklist_never_claims_the_packet_is_ready():
    text = _checklist_text({"opp_id": 24, "client_name": "Salem", "status": "evaluating"})
    assert "Do not submit" in text


def test_checklist_is_crlf_for_notepad():
    """Windows Notepad renders a lone LF as one unreadable line."""
    raw = studio._checklist({"opp_id": 1, "client_name": "x", "status": "y"}, [], [])
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_checklist_is_the_first_entry_in_the_zip():
    import inspect
    src = inspect.getsource(studio.export_materials_zip)
    assert '"00_SUBMISSION_CHECKLIST.txt"' in src, (
        "the checklist must sort above 01_narrative so it is what opens first"
    )


# ---------------------------------------------------------------------------
# 4. A form we filled in is a deliverable, not one of the agency's own files.
# ---------------------------------------------------------------------------

def test_completed_form_goes_with_the_other_completed_work():
    doc = {"doc_id": 601, "doc_role": "form",
           "filename": "Salem_Attachment_A_iteria_response.docx"}
    assert studio._packet_folder(doc) == "02_filled_forms/"


@pytest.mark.parametrize("role", ["rfp", "addendum", "questionnaire",
                                  "cost_workbook", None, ""])
def test_agency_files_stay_in_attachments(role):
    assert studio._packet_folder({"doc_role": role}) == "03_attachments/"


def test_form_role_is_read_loosely():
    assert studio._packet_folder({"doc_role": " Form "}) == "02_filled_forms/"


def test_the_blank_and_the_filled_form_do_not_land_side_by_side():
    """Salem ships both "Attachment A (2).docx" (theirs, blank) and
    "Salem_Attachment_A_iteria_response.docx" (ours, filled). One folder for
    both is how a blank form gets submitted."""
    blank = {"doc_role": "rfp", "filename": "008 Town of Salem ERP RFP - Attachment A (2).docx"}
    filled = {"doc_role": "form", "filename": "Salem_Attachment_A_iteria_response.docx"}
    assert studio._packet_folder(blank) != studio._packet_folder(filled)


def test_a_filled_form_is_never_withheld_as_internal():
    assert studio._is_internal_doc({"doc_role": "form", "promoted_to_lib": "N"}) is False


def test_a_promoted_form_is_still_a_deliverable():
    """Uploading the filled Attachment A auto-promoted it to the library, and
    the promotion check then withheld it. The one document needing a signature
    was the one silently missing from the packet."""
    doc = {"doc_id": 602, "doc_role": "form", "promoted_to_lib": "Y",
           "filename": "Jefferson_Attachment_A_iteria_response.docx"}
    assert studio._is_internal_doc(doc) is False
    assert studio._packet_folder(doc) == "02_filled_forms/"
