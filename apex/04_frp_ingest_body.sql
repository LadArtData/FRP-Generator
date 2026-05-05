-- ═══════════════════════════════════════════════════════════════════
-- FRP_INGEST — package body, clean rewrite
--
-- Each step is its own named function so failures point at exactly one thing:
--
--   fetch_blob(path)             → BLOB
--   extract_text(blob)           → CLOB
--   persist_doc(...)             → row in FRP_DOCS
--   chunk_text(text)             → SYS.ODCIVARCHAR2LIST  (just strings)
--   embed_one(chunk)             → VECTOR
--   embed_and_persist(doc, text) → loops chunks, embeds one at a time, inserts one at a time
--   ingest_object(path)          → orchestrator
--   scan_all_prefixes            → bulk driver
--   deal_status_for(path)        → prefix → status mapping
--   stable_doc_id(path)          → md5 of path
--
-- Why one-chunk-at-a-time? Because when an embed fails, we know which chunk,
-- which model call, which row. The old "TABLE(UTL_TO_EMBEDDINGS) + JSON_TABLE
-- + INSERT" mega-statement gives ORA-51804 with no clue which chunk caused it.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE BODY FRP_INGEST AS

  -- ─────────────────────────────────────────────────────────
  -- Map a bucket prefix to a deal status
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
  -- Stable, deterministic doc_id from the bucket path
  -- ─────────────────────────────────────────────────────────
  FUNCTION stable_doc_id(p_object_name IN VARCHAR2) RETURN VARCHAR2 IS
    l_hash VARCHAR2(64);
  BEGIN
    SELECT LOWER(STANDARD_HASH('frpstudio:' || p_object_name, 'MD5'))
      INTO l_hash FROM dual;
    RETURN l_hash;
  END stable_doc_id;

  -- ─────────────────────────────────────────────────────────
  -- Step 1: pull a file from the bucket as a BLOB
  -- ─────────────────────────────────────────────────────────
  FUNCTION fetch_blob(p_object_name IN VARCHAR2) RETURN BLOB IS
  BEGIN
    RETURN DBMS_CLOUD.GET_OBJECT(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      object_uri      => C_BUCKET_BASE || p_object_name
    );
  END;

  -- ─────────────────────────────────────────────────────────
  -- Step 2: extract plain text from any supported binary
  -- (PDF, DOCX, XLSX, HTML, TXT — handled by 23ai natively)
  -- ─────────────────────────────────────────────────────────
  FUNCTION extract_text(p_blob IN BLOB) RETURN CLOB IS
  BEGIN
    RETURN DBMS_VECTOR_CHAIN.UTL_TO_TEXT(p_blob, JSON('{ "plaintext": "true" }'));
  END;

  -- ─────────────────────────────────────────────────────────
  -- Step 3: upsert the FRP_DOCS row (delete-then-insert, by design —
  -- avoids a PL/SQL→SQL identifier-resolution quirk we hit with MERGE)
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
  -- Step 4a: chunk text into a list of strings (no embedding yet)
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
      -- UTL_TO_CHUNKS returns each chunk wrapped as JSON: {"chunk_id":N,"chunk_data":"..."}
      l_chunks(l_chunks.COUNT) := JSON_VALUE(rec.chunk_str, '$.chunk_data');
    END LOOP;
    RETURN l_chunks;
  END chunk_text;

  -- ─────────────────────────────────────────────────────────
  -- Step 4b: embed ONE chunk → VECTOR
  -- Single round-trip to OCI Generative AI per chunk. Slower than batching,
  -- but errors point at one chunk, not a whole batch.
  -- ─────────────────────────────────────────────────────────
  FUNCTION embed_one(p_chunk IN CLOB) RETURN VECTOR IS
    l_vec VECTOR;
  BEGIN
    SELECT DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING(
             p_chunk,
             JSON('{"provider":"ocigenai","credential_name":"OCI$RESOURCE_PRINCIPAL","url":"https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/20231130/actions/embedText","model":"cohere.embed-v4.0","input_type":"search_document"}')
           )
      INTO l_vec
      FROM dual;
    RETURN l_vec;
  END embed_one;

  -- ─────────────────────────────────────────────────────────
  -- Step 4c: chunk + embed + insert, one chunk at a time
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

    IF l_chunks.COUNT = 0 THEN
      RETURN;  -- empty doc, nothing to embed
    END IF;

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
  -- Orchestrator
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
  -- Bulk driver — scan every prefix, ingest each object,
  -- log success or failure to FRP_INGEST_LOG, commit per file.
  -- ─────────────────────────────────────────────────────────
  PROCEDURE scan_all_prefixes IS
    l_started TIMESTAMP;
    l_doc_id  VARCHAR2(64);
    l_chunks  NUMBER;
    l_size    NUMBER;
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

        SELECT COUNT(*)        INTO l_chunks FROM FRP_CHUNKS WHERE doc_id = l_doc_id;
        SELECT NVL(size_bytes,0) INTO l_size  FROM FRP_DOCS  WHERE doc_id = l_doc_id;

        INSERT INTO FRP_INGEST_LOG
          (bucket_path, doc_id, status, size_bytes, chunks, duration_ms, started_at, finished_at)
          VALUES (rec.object_name, l_doc_id, 'ok', l_size, l_chunks,
                  EXTRACT(SECOND FROM (SYSTIMESTAMP - l_started)) * 1000,
                  l_started, SYSTIMESTAMP);
        COMMIT;
      EXCEPTION WHEN OTHERS THEN
        ROLLBACK;
        INSERT INTO FRP_INGEST_LOG
          (bucket_path, status, error_msg, started_at, finished_at)
          VALUES (rec.object_name, 'failed',
                  SUBSTR(SQLERRM || ' || ' || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE, 1, 4000),
                  l_started, SYSTIMESTAMP);
        COMMIT;
      END;
    END LOOP;
  END scan_all_prefixes;

END FRP_INGEST;
/
