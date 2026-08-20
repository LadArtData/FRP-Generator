"""Regression tests for download filename headers.

Documents 451 and 452 returned 500 on every download attempt:

    'latin-1' codec can't encode characters in position 54-55

HTTP header values are latin-1. Any agency filename carrying a curly
apostrophe, an en dash, or an accented character crashed the response before a
byte of the file was sent. This is what "the download button doesn't work"
actually was, and it affected every download route in the application.
"""

import pytest

from app.main import _attachment


def _header(name):
    return _attachment(name)["Content-Disposition"]


def test_plain_ascii_filename_is_unchanged():
    header = _header("Attachment B.xlsx")
    assert 'filename="Attachment B.xlsx"' in header


@pytest.mark.parametrize("name", [
    "Jefferson County Sheriff’s Office RFP.docx",   # curly apostrophe
    "Salem – Attachment C.xlsx",                    # en dash
    "Municipalité de Québec RFP.pdf",          # accents
    "中文文件.docx",                     # non-latin script
    "café — résumé.docx",            # em dash and accents
])
def test_header_is_latin1_encodable(name):
    """The whole point: the header must survive the ASGI layer."""
    _header(name).encode("latin-1")


@pytest.mark.parametrize("name", [
    "Jefferson County Sheriff’s Office RFP.docx",
    "Salem – Attachment C.xlsx",
])
def test_real_name_is_preserved_in_rfc5987_field(name):
    from urllib.parse import quote
    header = _header(name)
    assert "filename*=UTF-8''" in header
    assert quote(name, safe="") in header


def test_quotes_in_filename_cannot_break_the_header():
    header = _header('weird "quoted" name.docx')
    # exactly two double quotes, the ones delimiting the ascii filename
    assert header.count('"') == 2


def test_empty_filename_falls_back():
    assert 'filename="download"' in _header("")
    assert 'filename="download"' in _header(None)


def test_ascii_fallback_still_present_for_unicode_names():
    """Old clients ignore filename* and need something readable."""
    header = _header("Salem – Attachment C.xlsx")
    assert 'filename="' in header
    assert header.index('filename="') < header.index("filename*=")


def test_every_download_route_uses_the_helper():
    """A new download route that formats its own header reintroduces the bug."""
    import inspect
    from app import main
    source = inspect.getsource(main)
    raw = source.count('"Content-Disposition": f\'attachment')
    assert raw == 0, "download routes must go through _attachment()"
    assert source.count("_attachment(filename)") >= 6
