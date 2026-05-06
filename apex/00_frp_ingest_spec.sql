-- ═══════════════════════════════════════════════════════════════════
-- FRP_INGEST — package spec
-- Run as ITERIA_AI in Database Actions → SQL.
-- Run BEFORE 04_frp_ingest_body.sql.
--
-- Adds three things the in-DB spec was missing:
--   1. embed_one (so 05_frp_ords_module.sql search endpoint can call it)
--   2. OCI compartment_id + GenAI URL + embed model as constants
--   3. C_BUCKET_BASE matches the live FRP_CONFIG.BUCKET_BASE value
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE FRP_INGEST AS

  C_BUCKET_BASE CONSTANT VARCHAR2(256) :=
    'https://objectstorage.ap-mumbai-1.oraclecloud.com/n/bmi3vxyqnzrv/b/FRPStudio/o/';

  C_OCI_COMPARTMENT_ID CONSTANT VARCHAR2(256) :=
    'ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2japj37aoagj6kcbfoa';

  C_GENAI_EMBED_URL CONSTANT VARCHAR2(256) :=
    'https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/20231130/actions/embedText';

  C_EMBED_MODEL CONSTANT VARCHAR2(64) :=
    'cohere.embed-english-light-v3.0';

  FUNCTION  deal_status_for(p_object_name IN VARCHAR2) RETURN VARCHAR2;
  FUNCTION  stable_doc_id  (p_object_name IN VARCHAR2) RETURN VARCHAR2;
  FUNCTION  embed_one      (p_chunk       IN CLOB)     RETURN VECTOR;
  PROCEDURE ingest_object  (p_object_name IN VARCHAR2);
  PROCEDURE scan_all_prefixes;

END FRP_INGEST;
/

SHOW ERRORS PACKAGE FRP_INGEST;
