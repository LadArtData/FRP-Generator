-- ═══════════════════════════════════════════════════════════════════
-- FRP_INGEST — package body
-- Run AFTER 00_frp_ingest_spec.sql.
--
-- Two changes from the in-DB version (which was INVALID):
--
-- 1. embed_one rewritten to use DBMS_CLOUD.SEND_REQUEST directly.
--    The previous version used DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING,
--    whose constructed request body OCI rejects with HTTP 400
--    "Please pass in correct format of request." — verified against
--    both cohere.embed-v4.0 and cohere.embed-english-light-v3.0.
--    Direct call against the same endpoint with the same auth returns
--    HTTP 200 + a 384-dim vector.
--
-- 2. scan_all_prefixes EXCEPTION handler captures FORMAT_ERROR_BACKTRACE
--    into a local variable before the INSERT. Inline use in a SQL
--    VALUES clause causes ORA-00984 ("column not allowed here") on
--    compile in this Oracle version.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE BODY FRP_INGEST AS

  -- ─────────────────────────────────────────────────────────
  FUNCTION deal_status_for(p_object_name IN VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    IF    p_object_name LIKE '01_BOILERPLATE/%'           THEN RETURN 'boilerplate';
    ELSIF p_object_name LIKE '02_WON_PROPOSALS/%'         THEN RETURN 'won';
    ELSIF p_object_name LIKE '03_IN_PROGRESS/%'           THEN RETURN 'in_progress';
    ELSIF p_object_name LIKE '04_LOST_PROPOSALS/%'        THEN RETURN 'lost';
    ELSIF p_object_name LIKE '05_SOURCE_RFPS/%'           THEN RETURN 'source';
    ELSIF p_object_name LIKE '06_COMPETITOR_INTEL/%'      THEN RETURN 'competitor';
    ELSIF p_object_name LIKE '08_DROP_ZONE/%'             THEN RETURN 'incoming';
    ELSIF p_object_name LIKE 'ERPs written by the tool/%' THEN RETURN 'tool_output';
    ELSIF p_object_name LIKE 'SharePoint_Upload_MSL/%'    THEN RETURN 'msl';
    ELSIF p_object_name LIKE 'SharePoint_Upload_NO-BID/%' THEN RETURN 'no_bid';
    ELSE RETURN 'other';
    END IF;
  END;

  -- ─────────────────────────────────────────────────────────
  FUNCTION stable_doc_id(p_object_name IN VARCHAR2) RETURN VARCHAR2 IS
    l_hash VARCHAR2(64);
  BEGIN
    SELECT LOWER(STANDARD_HASH('frpstudio:' || p_object_name, 'MD5'))
      INTO l_hash FROM dual;
    RETURN l_hash;
  END stable_doc_id;

  -- ─────────────────────────────────────────────────────────
  FUNCTION fetch_blob(p_object_name IN VARCHAR2) RETURN BLOB IS
  BEGIN
    RETURN DBMS_CLOUD.GET_OBJECT(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      object_uri      => C_BUCKET_BASE || p_object_name);
  END;

  -- ─────────────────────────────────────────────────────────
  FUNCTION extract_text(p_blob IN BLOB) RETURN CLOB IS
  BEGIN
    RETURN DBMS_VECTOR_CHAIN.UTL_TO_TEXT(p_blob, JSON('{ "plaintext": "true" }'));
  END;

  -- ─────────────────────────────────────────────────────────
  PROCEDURE persist_doc(
    p_doc_id      IN VARCHAR2,
    p_object_name IN VARCHAR2,
    p_size_bytes  IN NUMBER,
    p_text        IN CLOB
  ) IS
    v_filename VARCHAR2(512) := REGEXP_SUBSTR(p_object_name, '[^/]+$');
    v_prefix   VARCHAR2(64)  := REGEXP_SUBSTR(p_object_name, '^[^/]+/');
    v_status   VARCHAR2(32)  := deal_status_for(p_object_name);
  BEGIN
    DELETE FROM FRP_DOCS WHERE doc_id = p_doc_id;
    INSERT INTO FRP_DOCS
      (doc_id, bucket_path, prefix, filename, size_bytes, deal_status, raw_text)
      VALUES (p_doc_id, p_object_name, v_prefix, v_filename, p_size_bytes, v_status, p_text);
  END persist_doc;

  -- ─────────────────────────────────────────────────────────
  FUNCTION chunk_text(p_text IN CLOB) RETURN SYS.ODCIVARCHAR2LIST IS
    l_chunks SYS.ODCIVARCHAR2LIST := SYS.ODCIVARCHAR2LIST();
  BEGIN
    FOR rec IN (
      SELECT t.column_value AS chunk_str
        FROM TABLE(
          DBMS_VECTOR_CHAIN.UTL_TO_CHUNKS(
            p_text,
            JSON('{"by":"words","max":"800","overlap":"80","split":"recursively","language":"english","normalize":"all"}')
          )
        ) t
    ) LOOP
      l_chunks.EXTEND;
      l_chunks(l_chunks.COUNT) := JSON_VALUE(rec.chunk_str, '$.chunk_data');
    END LOOP;
    RETURN l_chunks;
  END chunk_text;

  -- ─────────────────────────────────────────────────────────
  -- Direct OCI HTTP call — bypasses DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING
  -- because the wrapper sends a body OCI returns HTTP 400 on.
  -- Verified working: cohere.embed-english-light-v3.0 → 384-dim vector.
  -- ─────────────────────────────────────────────────────────
  FUNCTION embed_one(p_chunk IN CLOB) RETURN VECTOR IS
    l_resp     DBMS_CLOUD_TYPES.RESP;
    l_body     CLOB;
    l_text     CLOB;
    l_status   NUMBER;
    l_vec      VECTOR;
  BEGIN
    l_body := JSON_OBJECT(
      'compartmentId' VALUE C_OCI_COMPARTMENT_ID,
      'servingMode'   VALUE JSON_OBJECT(
                              'servingType' VALUE 'ON_DEMAND',
                              'modelId'     VALUE C_EMBED_MODEL),
      'inputs'        VALUE JSON_ARRAY(p_chunk),
      'truncate'      VALUE 'END',
      'inputType'     VALUE 'SEARCH_DOCUMENT'
      RETURNING CLOB
    );

    l_resp := DBMS_CLOUD.SEND_REQUEST(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => C_GENAI_EMBED_URL,
      method          => 'POST',
      headers         => JSON_OBJECT('Content-Type' VALUE 'application/json'),
      body            => UTL_RAW.CAST_TO_RAW(l_body)
    );

    l_status := DBMS_CLOUD.GET_RESPONSE_STATUS_CODE(l_resp);
    l_text   := DBMS_CLOUD.GET_RESPONSE_TEXT(l_resp);

    IF l_status != 200 THEN
      RAISE_APPLICATION_ERROR(-20100,
        'OCI embed call failed (HTTP ' || l_status || '): ' ||
        SUBSTR(l_text, 1, 2000));
    END IF;

    SELECT TO_VECTOR(JSON_QUERY(l_text, '$.embeddings[0]' RETURNING CLOB))
      INTO l_vec FROM dual;

    RETURN l_vec;
  END embed_one;

  -- ─────────────────────────────────────────────────────────
  PROCEDURE embed_and_persist(
    p_doc_id IN VARCHAR2,
    p_text   IN CLOB
  ) IS
    l_chunks   SYS.ODCIVARCHAR2LIST;
    l_chunk    CLOB;
    l_vec      VECTOR;
    l_chunk_id VARCHAR2(64);
  BEGIN
    DELETE FROM FRP_CHUNKS WHERE doc_id = p_doc_id;
    l_chunks := chunk_text(p_text);

    IF l_chunks.COUNT = 0 THEN RETURN; END IF;

    FOR i IN 1 .. l_chunks.COUNT LOOP
      l_chunk := l_chunks(i);
      l_vec   := embed_one(l_chunk);

      SELECT LOWER(STANDARD_HASH(p_doc_id || ':' || i, 'MD5'))
        INTO l_chunk_id FROM dual;

      INSERT INTO FRP_CHUNKS
        (chunk_id, doc_id, chunk_index, chunk_text, token_count, embedding)
      VALUES
        (l_chunk_id, p_doc_id, i, TO_CLOB(l_chunk), LENGTH(l_chunk), l_vec);
    END LOOP;
  END embed_and_persist;

  -- ─────────────────────────────────────────────────────────
  PROCEDURE ingest_object(p_object_name IN VARCHAR2) IS
    v_blob   BLOB;
    v_text   CLOB;
    v_doc    VARCHAR2(64) := stable_doc_id(p_object_name);
    v_size   NUMBER;
  BEGIN
    v_blob := fetch_blob(p_object_name);
    v_size := DBMS_LOB.GETLENGTH(v_blob);
    v_text := extract_text(v_blob);

    persist_doc(v_doc, p_object_name, v_size, v_text);
    embed_and_persist(v_doc, v_text);

    DBMS_LOB.FREETEMPORARY(v_blob);
  END ingest_object;

  -- ─────────────────────────────────────────────────────────
  PROCEDURE scan_all_prefixes IS
    l_started TIMESTAMP;
    l_doc_id  VARCHAR2(64);
    l_chunks  NUMBER;
    l_size    NUMBER;
    l_err     VARCHAR2(4000);
  BEGIN
    FOR rec IN (
      SELECT object_name
        FROM DBMS_CLOUD.LIST_OBJECTS('OCI$RESOURCE_PRINCIPAL', C_BUCKET_BASE)
       WHERE object_name LIKE '01_BOILERPLATE/%'
          OR object_name LIKE '02_WON_PROPOSALS/%'
          OR object_name LIKE '03_IN_PROGRESS/%'
          OR object_name LIKE '04_LOST_PROPOSALS/%'
          OR object_name LIKE '05_SOURCE_RFPS/%'
          OR object_name LIKE '06_COMPETITOR_INTEL/%'
          OR object_name LIKE '08_DROP_ZONE/%'
          OR object_name LIKE 'ERPs written by the tool/%'
          OR object_name LIKE 'SharePoint_Upload_MSL/%'
          OR object_name LIKE 'SharePoint_Upload_NO-BID/%'
    ) LOOP
      l_started := SYSTIMESTAMP;
      BEGIN
        ingest_object(rec.object_name);
        l_doc_id := stable_doc_id(rec.object_name);

        SELECT COUNT(*)          INTO l_chunks FROM FRP_CHUNKS WHERE doc_id = l_doc_id;
        SELECT NVL(size_bytes,0) INTO l_size   FROM FRP_DOCS  WHERE doc_id = l_doc_id;

        INSERT INTO FRP_INGEST_LOG
          (bucket_path, doc_id, status, size_bytes, chunks, duration_ms, started_at, finished_at)
          VALUES (rec.object_name, l_doc_id, 'ok', l_size, l_chunks,
                  EXTRACT(SECOND FROM (SYSTIMESTAMP - l_started)) * 1000,
                  l_started, SYSTIMESTAMP);
        COMMIT;
      EXCEPTION WHEN OTHERS THEN
        ROLLBACK;
        l_err := SUBSTR(SQLERRM || ' || ' || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE, 1, 4000);
        INSERT INTO FRP_INGEST_LOG
          (bucket_path, status, error_msg, started_at, finished_at)
          VALUES (rec.object_name, 'failed', l_err, l_started, SYSTIMESTAMP);
        COMMIT;
      END;
    END LOOP;
  END scan_all_prefixes;

END FRP_INGEST;
/

SHOW ERRORS PACKAGE BODY FRP_INGEST;
