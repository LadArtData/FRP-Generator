"""Configuration guards.

Both cases here are real. The tenancy is in ap-mumbai-1, which is where the
database lives, but the Llama model is served from us-chicago-1 and its OCID
says so. GENAI_REGION falls back to OCI_REGION when unset, so setting only
OCI_REGION builds a Mumbai endpoint for a Chicago model. OCI answers that with
a 404 on the model and no mention of a region.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config, ConfigError, _region_from_ocid  # noqa: E402

CHICAGO_MODEL = ("ocid1.generativeaimodel.oc1.us-chicago-1."
                 "amaaaaaask7dceya2xrydihzvu5pk6vlvfhtbnfapcvwhhugzo7jez4zcnaa")
TENANCY = ("ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2"
           "japj37aoagj6kcbfoa")

BASE = {
    "ORACLE_PASSWORD": "x",
    "ORACLE_DSN": "harald_high",
    "GENAI_MODEL_OCID": CHICAGO_MODEL,
    "GENAI_COMPARTMENT_ID": TENANCY,
    "HARALD_SESSION_SECRET": "a-long-random-session-secret",
}


def build(monkeypatch, **overrides) -> Config:
    for key in ("GENAI_REGION", "OCI_REGION", "GENAI_COMPARTMENT_ID",
                "OCI_COMPARTMENT_ID", "HARALD_POLISH_MODEL_OCID",
                "HARALD_DRAFT_MODEL_OCID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Config()


class TestRegionFromOcid:
    def test_model_ocid_carries_its_region(self):
        assert _region_from_ocid(CHICAGO_MODEL) == "us-chicago-1"

    def test_tenancy_ocid_has_no_region(self):
        assert _region_from_ocid(TENANCY) == ""


class TestRegionConsistency:
    def test_matching_region_validates(self, monkeypatch):
        cfg = build(monkeypatch, GENAI_REGION="us-chicago-1")
        cfg.validate()
        assert cfg.genai_endpoint == (
            "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com")

    def test_tenancy_home_region_alone_is_rejected(self, monkeypatch):
        """Setting only OCI_REGION is the mistake this guard exists for."""
        cfg = build(monkeypatch, OCI_REGION="ap-mumbai-1")
        with pytest.raises(ConfigError) as exc:
            cfg.validate()
        message = str(exc.value)
        assert "us-chicago-1" in message and "ap-mumbai-1" in message
        assert "GENAI_REGION" in message

    def test_genai_region_wins_over_oci_region(self, monkeypatch):
        cfg = build(monkeypatch, GENAI_REGION="us-chicago-1",
                    OCI_REGION="ap-mumbai-1")
        cfg.validate()
        assert cfg.genai_region == "us-chicago-1"


class TestCompartment:
    def test_tenancy_ocid_is_accepted_as_the_root_compartment(self, monkeypatch):
        build(monkeypatch, GENAI_REGION="us-chicago-1").validate()

    def test_compartment_ocid_is_accepted(self, monkeypatch):
        build(monkeypatch, GENAI_REGION="us-chicago-1",
              GENAI_COMPARTMENT_ID="ocid1.compartment.oc1..aaaa").validate()

    def test_a_pasted_model_ocid_is_rejected(self, monkeypatch):
        cfg = build(monkeypatch, GENAI_REGION="us-chicago-1",
                    GENAI_COMPARTMENT_ID=CHICAGO_MODEL)
        with pytest.raises(ConfigError, match="compartment or tenancy OCID"):
            cfg.validate()

    def test_ondemand_model_id_in_the_model_slot_is_accepted(self, monkeypatch):
        # On-demand inference accepts the OCI model id string, not only OCIDs.
        build(monkeypatch, GENAI_REGION="us-chicago-1",
              GENAI_MODEL_OCID="meta.llama-3.3-70b-instruct").validate()

    def test_garbage_model_id_is_rejected(self, monkeypatch):
        cfg = build(monkeypatch, GENAI_REGION="us-chicago-1",
                    GENAI_MODEL_OCID="not-a-real-model")
        with pytest.raises(ConfigError, match="model OCID|on-demand model id"):
            cfg.validate()
