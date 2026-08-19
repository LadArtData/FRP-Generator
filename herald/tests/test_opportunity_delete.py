"""Regression test for deleting an opportunity that has extracted requirements.

Deleting the duplicate TTUHSC proposal 82 failed with:

    ORA-02292: integrity constraint violated - child record found

Four tables reference harald_documents and only harald_chunks cascades. The
delete removed document rows while eleven harald_requirements rows still
carried source_doc_id pointing at one of them, so the whole transaction rolled
back and the record could not be removed at all.

There is no live database in the test environment, so this asserts the shape of
the statements delete() issues and, most importantly, their order: every
reference must be cleared or removed before the document row it points at.
"""

import re

from app import opportunities


SOURCE = re.sub(r"\s+", " ", opportunities.delete.__doc__ or "")


def _delete_source() -> str:
    import inspect
    return inspect.getsource(opportunities.delete)


def test_clears_requirement_pointers_before_deleting_documents():
    src = _delete_source()
    null_req = src.index("UPDATE harald_requirements SET source_doc_id = NULL")
    delete_doc = src.index("DELETE FROM harald_documents")
    assert null_req < delete_doc, (
        "requirements must release the document before it is deleted, or "
        "ORA-02292 rolls the whole delete back"
    )


def test_removes_questionnaires_before_deleting_documents():
    """questionnaires.source_doc_id is NOT NULL, so it cannot be released --
    the questionnaire has to go with its workbook."""
    src = _delete_source()
    del_q = src.index("DELETE FROM harald_questionnaires WHERE source_doc_id")
    del_doc = src.index("DELETE FROM harald_documents")
    assert del_q < del_doc


def test_clears_supersedes_pointers_before_deleting_documents():
    src = _delete_source()
    null_sup = src.index("UPDATE harald_documents SET supersedes_id = NULL")
    del_doc = src.index("DELETE FROM harald_documents")
    assert null_sup < del_doc


def test_clears_rfp_doc_pointers_from_other_opportunities():
    src = _delete_source()
    assert "UPDATE harald_opportunities SET rfp_doc_id = NULL WHERE rfp_doc_id" in src


def test_opportunity_row_is_removed_last():
    src = _delete_source()
    del_doc = src.index("DELETE FROM harald_documents")
    del_opp = src.index("DELETE FROM harald_opportunities")
    assert del_doc < del_opp


def test_every_non_cascading_reference_to_documents_is_handled():
    """If a new table starts referencing harald_documents, this test is the
    reminder that delete() has to learn about it."""
    src = _delete_source()
    for table in ("harald_requirements", "harald_questionnaires",
                  "harald_opportunities", "harald_documents"):
        assert table in src, f"{table} references documents and is unhandled"
