"""Classification, voice scoring and the pre-submission gate.

These cover the merged HARALD quality layer. The cases are drawn from what the
real nine-document library actually did, not from invented fixtures.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ORACLE_PASSWORD", "x")
os.environ.setdefault("ORACLE_DSN", "x")
os.environ.setdefault("GENAI_REGION", "us-chicago-1")
os.environ.setdefault("GENAI_MODEL_OCID", "ocid1.generativeaimodel.oc1.test.aaaa")
os.environ.setdefault("GENAI_COMPARTMENT_ID", "ocid1.compartment.oc1..test")
os.environ.setdefault("HARALD_SESSION_SECRET", "unit-test-secret-value")
os.environ.setdefault("HARALD_APPROVER_PASSPHRASE", "unit-test-pass")

from app import classifier, gate, voice  # noqa: E402

# Condensed from West Fargo Attachment C2, which reads as a proposal by its
# name and its topic and is in fact the agency's own instructions.
AGENCY_TEXT = (
    "Attachment C2 Cost Narrative. Proposer is instructed to complete and submit "
    "the Price Proposal under separate cover as identified in the RFP schedule. "
    "Proposers shall submit the cost worksheets contained in Attachment C1. "
    "The City reserves the right to reject any proposal. Failure to comply with "
    "these instructions shall result in the proposal being deemed non-responsive. "
    "Proposer shall provide a narrative describing all assumptions underlying the "
    "pricing. The Proposer must include all travel costs. "
) * 6

VENDOR_TEXT = (
    "iteria is pleased to submit this response. We propose a phased implementation "
    "beginning with core financials. Our team has delivered Oracle Cloud for county "
    "government. iteria will staff the project with a dedicated functional lead. "
    "We understand the City's requirement for fund accounting. Our approach reduces "
    "risk by sequencing the payroll parallel early. "
) * 6


class TestBodyClassification:
    def test_agency_instructions_are_not_iteria_narrative(self):
        assert classifier.classify("WestFargo_AttachmentC2.docx", AGENCY_TEXT) \
            == classifier.CLIENT_RFP

    def test_vendor_response_is_narrative(self):
        assert classifier.classify("WestFargo_Technical.docx", VENDOR_TEXT) \
            == classifier.ITERIA_NARRATIVE

    def test_body_overrides_a_filename_with_no_signal(self):
        # None of the nine real filenames contain "iteria", so the path rule
        # returns UNCLASSIFIED for all of them.
        assert classifier.classify_path("BrownCounty_Proposal.docx") \
            == classifier.UNCLASSIFIED
        assert classifier.classify("BrownCounty_Proposal.docx", VENDOR_TEXT) \
            == classifier.ITERIA_NARRATIVE

    def test_provenance_classes_still_win_over_body(self):
        # A competitor's proposal reads exactly like a proposal, so the body
        # must not be allowed to promote it into the voice corpus.
        assert classifier.classify("CanAm_Response.docx", VENDOR_TEXT) \
            == classifier.COMPETITOR
        assert classifier.classify("w-9_form.pdf", VENDOR_TEXT) == classifier.ADMIN

    def test_short_text_falls_back_to_the_filename(self):
        assert classifier.classify("City_RFP_2026.docx", "Too short to measure.") \
            == classifier.CLIENT_RFP


class TestVoiceScoring:
    def test_banned_words_are_found_with_replacements(self):
        findings = voice.score(
            "We leverage a robust and comprehensive solution to ensure success.")
        assert "leverage" in findings["banned_words"]
        assert "robust" in findings["banned_words"]
        assert not findings["clean"]
        for word in findings["banned_words"]:
            assert voice.BANNED[word], f"{word} has no replacement to offer"

    def test_em_dashes_are_flagged(self):
        findings = voice.score("The timeline runs 12\u201324 months\u2014as scoped.")
        assert findings["forbidden_chars"]

    def test_dropped_placeholder_is_blocking(self):
        draft = "iteria will begin in [START DATE] with the core team."
        final = "iteria will begin promptly with the core team."
        findings = voice.score(final, draft=draft)
        assert findings["placeholders_dropped"] == ["[START DATE]"]
        assert findings["blocking"], "a silently dropped placeholder must block"

    def test_invented_number_is_blocking(self):
        draft = "The project runs in phases."
        final = "The project runs 47 weeks and started in 2019."
        findings = voice.score(final, draft=draft)
        assert findings["unsourced_numbers"]
        assert findings["blocking"]

    def test_numbers_present_in_the_source_are_not_flagged(self):
        draft = "The project runs 24 months from award."
        final = "The project runs 24 months from award."
        assert not voice.score(final, draft=draft)["unsourced_numbers"]

    def test_repair_brief_names_the_specific_word(self):
        findings = voice.score("We leverage the platform to ensure delivery.")
        brief = voice.repair_brief(findings)
        assert brief and "leverage" in brief
        assert voice.repair_brief({"clean": True}) is None


class TestGate:
    def test_rhythm_is_not_judged_on_a_small_sample(self):
        """A 352-word executive summary failed on rhythm before this guard.
        A distribution needs a sample."""
        short = "iteria will deliver the core financials release. " * 12
        text, _ = gate.read_bytes("note.txt", short.encode())
        findings = voice.score(text)
        assert findings["rhythm"]["sentences"] < gate.MIN_RHYTHM_SENTENCES

    def test_agency_and_vendor_rates_separate_the_two_voices(self):
        agency_a, vendor_a = classifier.voice_rates(AGENCY_TEXT)
        agency_v, vendor_v = classifier.voice_rates(VENDOR_TEXT)
        assert agency_a > vendor_a
        assert vendor_v > agency_v

    def test_reads_txt_and_finds_headings(self):
        raw = b"TECHNICAL APPROACH\niteria will configure the general ledger.\n"
        text, sections = gate.read_bytes("x.txt", raw)
        assert "general ledger" in text
        assert sections and sections[0][0] == "TECHNICAL APPROACH"
