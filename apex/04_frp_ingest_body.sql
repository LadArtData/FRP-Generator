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
  -- Direct OCI HTTP call — bypasses DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING
  -- because the wrapper sends a body OCI returns HTTP 400 on.
  -- Verified working: cohere.embed-english-light-v3.0 → 384-dim vector.
  -- ─────────────────────────────────────────────────────────
  FUNCTION embed_one(p_chunk IN CLOB) RETURN VECTOR IS
    l_resp     DBMS_CLOUD_TYPES.RESP;
    l_body     VARCHAR2(32767);
    l_text     CLOB;
    l_status   NUMBER;
    l_vec      VECTOR;
    j_root     JSON_OBJECT_T := JSON_OBJECT_T();
    j_serving  JSON_OBJECT_T := JSON_OBJECT_T();
    j_inputs   JSON_ARRAY_T  := JSON_ARRAY_T();
  BEGIN
    j_serving.put('servingType', 'ON_DEMAND');
    j_serving.put('modelId',     C_EMBED_MODEL);

    j_inputs.append(SUBSTR(p_chunk, 1, 30000));

    j_root.put('compartmentId', C_OCI_COMPARTMENT_ID);
    j_root.put('servingMode',   j_serving);
    j_root.put('inputs',        j_inputs);
    j_root.put('truncate',      'END');
    j_root.put('inputType',     'SEARCH_DOCUMENT');

    l_body := j_root.to_string;

    l_resp := DBMS_CLOUD.SEND_REQUEST(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => C_GENAI_EMBED_URL,
      method          => 'POST',
      headers         => '{"Content-Type":"application/json"}',
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
  -- ─────────────────────────────────────────────────────────
  -- parse_rfp — extract structured fields from RFP text via Llama.
  -- Returns a JSON CLOB with client_name, industry, primary_contact,
  -- annual_budget, current_erp, pain_points[], rfp_number, due_date,
  -- scope_summary. Unknown fields come back as null.
  -- Input is truncated to 20000 chars to keep request body within
  -- VARCHAR2(32767) ceiling after JSON-escape overhead.
  -- ─────────────────────────────────────────────────────────
  FUNCTION parse_rfp(p_text IN CLOB) RETURN CLOB IS
    l_resp     DBMS_CLOUD_TYPES.RESP;
    l_body     VARCHAR2(32767);
    l_text     CLOB;
    l_status   NUMBER;
    l_input    VARCHAR2(8000 CHAR);
    l_prompt   VARCHAR2(10000 CHAR);
    l_result   CLOB;

    j_root     JSON_OBJECT_T := JSON_OBJECT_T();
    j_serving  JSON_OBJECT_T := JSON_OBJECT_T();
    j_chat     JSON_OBJECT_T := JSON_OBJECT_T();
    j_messages JSON_ARRAY_T  := JSON_ARRAY_T();
    j_msg      JSON_OBJECT_T := JSON_OBJECT_T();
    j_content  JSON_ARRAY_T  := JSON_ARRAY_T();
    j_part     JSON_OBJECT_T := JSON_OBJECT_T();
  BEGIN
    l_input := DBMS_LOB.SUBSTR(p_text, 8000, 1);

    l_prompt :=
      'Extract structured fields from this RFP as JSON. ' ||
      'Required fields: client_name, industry, ' ||
      'primary_contact {name, title, email}, annual_budget, current_erp, ' ||
      'pain_points (array of short strings), rfp_number, ' ||
      'due_date (YYYY-MM-DD or null), scope_summary (one paragraph). ' ||
      'Use null for any field you cannot confirm from the text. ' ||
      'Return only valid JSON, no preamble, no markdown fences.' || CHR(10) || CHR(10) ||
      'RFP TEXT:' || CHR(10) ||
      l_input || CHR(10) || CHR(10) ||
      'JSON:';

    j_part.put('type', 'TEXT');
    j_part.put('text', l_prompt);
    j_content.append(j_part);

    j_msg.put('role',    'USER');
    j_msg.put('content', j_content);
    j_messages.append(j_msg);

    j_chat.put('apiFormat',   'GENERIC');
    j_chat.put('messages',    j_messages);
    j_chat.put('maxTokens',   1500);
    j_chat.put('temperature', 0.1);
    j_chat.put('topP',        0.9);
    j_chat.put('isStream',    FALSE);

    j_serving.put('servingType', 'ON_DEMAND');
    j_serving.put('modelId',     C_PARSER_MODEL_OCID);

    j_root.put('compartmentId', C_OCI_COMPARTMENT_ID);
    j_root.put('servingMode',   j_serving);
    j_root.put('chatRequest',   j_chat);

    l_body := j_root.to_string;

    l_resp := DBMS_CLOUD.SEND_REQUEST(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => C_GENAI_CHAT_URL,
      method          => 'POST',
      headers         => '{"Content-Type":"application/json"}',
      body            => UTL_RAW.CAST_TO_RAW(l_body)
    );

    l_status := DBMS_CLOUD.GET_RESPONSE_STATUS_CODE(l_resp);
    l_text   := DBMS_CLOUD.GET_RESPONSE_TEXT(l_resp);

    IF l_status != 200 THEN
      RAISE_APPLICATION_ERROR(-20101,
        'OCI chat (parse_rfp) failed (HTTP ' || l_status || '): ' ||
        SUBSTR(l_text, 1, 2000));
    END IF;

    l_result := JSON_VALUE(l_text,
      '$.chatResponse.choices[0].message.content[0].text' RETURNING CLOB);

    RETURN l_result;
  END parse_rfp;

  -- Chunk + embed + insert in a single pass — no intermediate collection.
  -- Chunks held only in CLOB locals so 800-word chunks (~5-6KB) don't hit
  -- VARCHAR2(4000) limits inside SYS.ODCIVARCHAR2LIST.
  PROCEDURE embed_and_persist(
    p_doc_id IN VARCHAR2,
    p_text   IN CLOB
  ) IS
    l_chunk    CLOB;
    l_vec      VECTOR;
    l_chunk_id VARCHAR2(64);
    l_idx      NUMBER := 0;
  BEGIN
    DELETE FROM FRP_CHUNKS WHERE doc_id = p_doc_id;

    FOR rec IN (
      SELECT t.column_value AS chunk_str
        FROM TABLE(
          DBMS_VECTOR_CHAIN.UTL_TO_CHUNKS(
            p_text,
            JSON('{"by":"words","max":"800","overlap":"80","split":"recursively","language":"english","normalize":"all"}')
          )
        ) t
    ) LOOP
      l_idx := l_idx + 1;
      l_chunk := JSON_VALUE(rec.chunk_str, '$.chunk_data' RETURNING CLOB);

      IF l_chunk IS NULL OR DBMS_LOB.GETLENGTH(l_chunk) = 0 THEN
        CONTINUE;
      END IF;

      l_vec := embed_one(l_chunk);

      SELECT LOWER(STANDARD_HASH(p_doc_id || ':' || l_idx, 'MD5'))
        INTO l_chunk_id FROM dual;

      INSERT INTO FRP_CHUNKS
        (chunk_id, doc_id, chunk_index, chunk_text, token_count, embedding)
      VALUES
        (l_chunk_id, p_doc_id, l_idx, l_chunk, DBMS_LOB.GETLENGTH(l_chunk), l_vec);
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
