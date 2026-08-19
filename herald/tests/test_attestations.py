"""Tests for the attestation check.

Every case here is taken from a real sentence in a real submitted draft. If the
check had existed, none of the three would have shipped.
"""

from app import attestations


def _questionnaire(filename, *, total, answered, placeable=None, abstained=0,
                   q_id=1, code_columns=None):
    placeable = total if placeable is None else placeable
    items = []
    for index in range(total):
        answered_here = index < answered
        code = ""
        if answered_here:
            code = "No Bid" if index < abstained else "Standard"
        items.append({
            "sheet": "Sheet1",
            "row": index + 2,
            "response_code": code,
            "response_col": "D" if index < placeable else None,
            "comment_col": None,
        })
    sheet_map = [{"sheet": "Sheet1", "code_columns": code_columns or {}}]
    return {"q_id": q_id, "filename": filename, "items": items,
            "sheet_map": sheet_map}


# ---------------------------------------------------------------------------
# Nashua: the workbook held 45 of ~3,000 requirements.
# ---------------------------------------------------------------------------

NASHUA_SENTENCE = (
    "We understand that these worksheets are weighted and scored. "
    "They have been completed by iteria functional leads accordingly."
)


def test_catches_completion_claim_over_a_mostly_empty_workbook():
    q = _questionnaire("5260 Appendix A.xlsx", total=3000, answered=45)
    findings = attestations.check(
        NASHUA_SENTENCE + " See Appendix A for detail.", [q])
    assert findings
    assert findings[0]["kind"] == "claims_complete_but_unanswered"
    assert findings[0]["answered"] == 45
    assert findings[0]["total"] == 3000


def test_completion_claim_passes_when_the_workbook_is_actually_complete():
    q = _questionnaire("5260 Appendix A.xlsx", total=3000, answered=3000)
    assert attestations.check(
        NASHUA_SENTENCE + " See Appendix A for detail.", [q]) == []


# ---------------------------------------------------------------------------
# Jefferson: narrative claimed abstention, workbook answered anyway.
# ---------------------------------------------------------------------------

JEFFERSON_ABSTENTION = (
    "Requirements falling within the property tax collection areas are marked "
    "as not proposed rather than answered, so the evaluation is not distorted. "
    "This applies throughout Attachment B."
)


def test_catches_abstention_claim_when_the_workbook_answered_anyway():
    q = _questionnaire("Attachment B.xlsx", total=167, answered=141, abstained=0)
    findings = attestations.check(JEFFERSON_ABSTENTION, [q])
    assert findings
    assert findings[0]["kind"] == "claims_abstention_but_answered"
    assert findings[0]["answered"] == 141


def test_abstention_claim_passes_when_rows_really_are_marked_no_bid():
    q = _questionnaire("Attachment B.xlsx", total=167, answered=167, abstained=167)
    assert attestations.check(JEFFERSON_ABSTENTION, [q]) == []


# ---------------------------------------------------------------------------
# Jefferson: "We have priced the SaaS model on Attachment C1." C1 was empty.
# ---------------------------------------------------------------------------

def test_catches_pricing_claim_over_an_empty_cost_workbook():
    q = _questionnaire("Attachment C1.xlsx", total=0, answered=0)
    findings = attestations.check(
        "We have priced the SaaS model on Attachment C1.", [q])
    assert findings
    assert findings[0]["kind"] == "claims_complete_but_empty"


def test_catches_completion_claim_when_nothing_can_be_written():
    """A cost worksheet with no response column: 187 answers, nowhere to put
    them. The export hands back the agency's blank original."""
    q = _questionnaire("Attachment C1.xlsx", total=187, answered=187, placeable=0)
    findings = attestations.check(
        "The cost worksheets in Attachment C1 have been completed.", [q])
    assert findings
    assert findings[0]["kind"] == "claims_complete_but_unwritable"
    assert findings[0]["placeable"] == 0


# ---------------------------------------------------------------------------
# Scoping and noise control.
# ---------------------------------------------------------------------------

def test_ignores_sentences_that_name_no_attachment():
    q = _questionnaire("Attachment B.xlsx", total=100, answered=1)
    assert attestations.check(
        "Our implementation approach has been completed in prior engagements.",
        [q]) == []


def test_ignores_sentences_about_a_different_attachment():
    b = _questionnaire("Attachment B.xlsx", total=100, answered=100, q_id=1)
    c = _questionnaire("Attachment C1.xlsx", total=10, answered=0, q_id=2)
    findings = attestations.check(
        "Attachment B has been completed in full.", [b, c])
    assert findings == []


def test_matrix_layout_counts_as_placeable():
    """A rating matrix has no response column but is entirely writable."""
    q = _questionnaire("Appendix A.xlsx", total=50, answered=50, placeable=0,
                       code_columns={"SUP": "D", "MOD": "E", "NS": "F"})
    assert attestations.check("Appendix A has been completed.", [q]) == []


def test_no_questionnaires_means_no_findings():
    assert attestations.check("Attachment B has been completed.", []) == []


def test_empty_draft_is_safe():
    q = _questionnaire("Attachment B.xlsx", total=10, answered=0)
    assert attestations.check("", [q]) == []


def test_finding_carries_the_offending_sentence_and_line():
    q = _questionnaire("Attachment C1.xlsx", total=0, answered=0)
    draft = "Intro line.\nWe have priced the SaaS model on Attachment C1."
    findings = attestations.check(draft, [q])
    assert findings[0]["line"] == 2
    assert "Attachment C1" in findings[0]["sentence"]
    assert findings[0]["q_id"] == 1


def test_summarise_reports_the_numbers_an_operator_needs():
    q = _questionnaire("Attachment B.xlsx", total=200, answered=50, placeable=180,
                       abstained=10)
    summary = attestations.summarise(q)
    assert summary["total"] == 200
    assert summary["answered"] == 50
    assert summary["abstained"] == 10
    assert summary["placeable"] == 180
    assert round(summary["answered_ratio"], 3) == 0.25


# ---------------------------------------------------------------------------
# Under-extraction: every ratio reads as perfect, and the import saw 1.5% of
# the workbook. Nashua's Appendix A was 45 rows across 23 functional tabs.
# ---------------------------------------------------------------------------

def _multi_sheet(filename, *, sheets, items_per_sheet, q_id=1):
    items = []
    for sheet_index in range(sheets):
        for row in range(items_per_sheet):
            items.append({
                "sheet": f"Tab{sheet_index}", "row": row + 12,
                "response_code": "SUP", "response_col": "D", "comment_col": "J",
            })
    sheet_map = [{"sheet": f"Tab{i}", "detected": True, "code_columns": {}}
                 for i in range(sheets)]
    return {"q_id": q_id, "filename": filename, "items": items,
            "sheet_map": sheet_map}


def test_catches_completion_claim_over_a_thin_extraction():
    q = _multi_sheet("5260 Appendix A.xlsx", sheets=23, items_per_sheet=2)
    findings = attestations.check(
        "The Appendix A worksheets have been completed by iteria functional leads.",
        [q])
    assert findings
    assert findings[0]["kind"] == "claims_complete_but_thin"
    assert findings[0]["total"] == 46


def test_thin_check_passes_on_a_properly_extracted_workbook():
    q = _multi_sheet("5260 Appendix A.xlsx", sheets=23, items_per_sheet=130)
    assert attestations.check(
        "The Appendix A worksheets have been completed by iteria functional leads.",
        [q]) == []


def test_thin_check_does_not_fire_on_a_genuinely_small_workbook():
    """A two-tab interface sheet really can hold a handful of rows."""
    q = _multi_sheet("Attachment B.xlsx", sheets=2, items_per_sheet=3)
    assert attestations.check("Attachment B has been completed.", [q]) == []
