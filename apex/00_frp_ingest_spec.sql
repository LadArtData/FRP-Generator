-- ═══════════════════════════════════════════════════════════════════
-- FRP_INGEST — package spec
-- Run as ITERIA_AI in Database Actions → SQL.
-- Run BEFORE 04_frp_ingest_body.sql.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE FRP_INGEST AS

  -- Bucket base URL — used by fetch_blob and scan_all_prefixes.
  -- Format: https://objectstorage.<region>.oraclecloud.com/n/<namespace>/b/<bucket>/o/
  C_BUCKET_BASE CONSTANT VARCHAR2(512) :=
    'https://objectstorage.ap-mumbai-1.oraclecloud.com/n/bmi3vxyqnzrv/b/FRPStudio/o/';

  -- prefix → deal_status mapping
  FUNCTION  deal_status_for(p_object_name IN VARCHAR2) RETURN VARCHAR2;

  -- md5 of path — same path always yields same doc_id
  FUNCTION  stable_doc_id(p_object_name IN VARCHAR2) RETURN VARCHAR2;

  -- Step 1: fetch object as BLOB
  FUNCTION  fetch_blob(p_object_name IN VARCHAR2) RETURN BLOB;

  -- Step 2: BLOB → CLOB plain text (PDF/DOCX/XLSX/HTML/TXT, native 23ai)
  FUNCTION  extract_text(p_blob IN BLOB) RETURN CLOB;

  -- Step 3: upsert FRP_DOCS row
  PROCEDURE persist_doc(
    p_doc_id      IN VARCHAR2,
    p_object_name IN VARCHAR2,
    p_size_bytes  IN NUMBER,
    p_text        IN CLOB
  );

  -- Step 4a: chunk a CLOB into a list of strings (no embedding)
  FUNCTION  chunk_text(p_text IN CLOB) RETURN SYS.ODCIVARCHAR2LIST;

  -- Step 4b: embed ONE chunk → VECTOR. Also called by /search at query time.
  FUNCTION  embed_one(p_chunk IN CLOB) RETURN VECTOR;

  -- Step 4c: chunk + embed + insert FRP_CHUNKS rows
  PROCEDURE embed_and_persist(p_doc_id IN VARCHAR2, p_text IN CLOB);

  -- Orchestrator for a single object
  PROCEDURE ingest_object(p_object_name IN VARCHAR2);

  -- Bulk driver — scan every known prefix and ingest every object
  PROCEDURE scan_all_prefixes;

END FRP_INGEST;
/

SHOW ERRORS PACKAGE FRP_INGEST;
