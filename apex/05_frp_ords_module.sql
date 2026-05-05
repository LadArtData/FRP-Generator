-- ═══════════════════════════════════════════════════════════════════
-- FRP Studio — ORDS module (frp-hooks)
-- Run as ITERIA_AI in Database Actions → SQL.
--
-- Mirrors the validate-hooks pattern (api_key gate, blob_to_clob_utf8
-- for bodies, log_ords_error for failures, manual JSON construction
-- so 19c ADB doesn't trip on JSON_OBJECT 32K limits).
--
-- Endpoints (12):
--   GET  health                       — sanity ping
--   GET  library                      — list FRP_DOCS, filter+search+limit
--   GET  library/stats                — counts for the rail footer
--   POST library/scan                 — kick FRP_INGEST.scan_all_prefixes
--   GET  library/:doc_id              — single doc + chunk count
--   POST search                       — vector search across FRP_CHUNKS
--   GET  proposals                    — list FRP_PROPOSALS
--   POST proposals                    — create draft proposal
--   GET  proposals/:id                — fetch one (extracted_json + draft_text)
--   POST proposals/:id/generate       — queue FRP_PIPELINE.run_proposal
--   POST proposals/:id/attachments    — link a library doc to a proposal
--   GET  bucket/folders               — prefix counts (deal_status histogram)
--
-- Auth: every handler reads :api_key from query string and compares to
-- iteria_ai.api_configuration. If no row exists in api_configuration
-- (is_active=Y), auth is skipped — same fail-open behaviour as Validate.
-- ═══════════════════════════════════════════════════════════════════

DECLARE
  l_dummy NUMBER;
BEGIN
  -- Schema enable is idempotent; safe to re-run.
  ORDS.ENABLE_SCHEMA(
      p_enabled             => TRUE,
      p_url_mapping_type    => 'BASE_PATH',
      p_url_mapping_pattern => 'admin',
      p_auto_rest_auth      => FALSE);

  ORDS.DEFINE_MODULE(
      p_module_name    => 'frp-hooks',
      p_base_path      => '/frp-hooks/',
      p_items_per_page => 0,
      p_status         => 'PUBLISHED',
      p_comments       => 'FRP Studio hooks — library, proposals, ingest');

  -- ─────────────────────────────────────────────────────────
  -- GET health
  -- Returns {ok:true, docs:N, chunks:N, proposals:N, now:"..."}
  -- so the frontend bridge can prove ORDS is reachable + auth works.
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'health',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Sanity ping for the bridge JS');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'health',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key  VARCHAR2(200);
  l_req_key  VARCHAR2(200) := :api_key;
  l_docs     NUMBER := 0;
  l_chunks   NUMBER := 0;
  l_props    NUMBER := 0;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  BEGIN SELECT COUNT(*) INTO l_docs   FROM iteria_ai.frp_docs;       EXCEPTION WHEN OTHERS THEN l_docs := -1; END;
  BEGIN SELECT COUNT(*) INTO l_chunks FROM iteria_ai.frp_chunks;     EXCEPTION WHEN OTHERS THEN l_chunks := -1; END;
  BEGIN SELECT COUNT(*) INTO l_props  FROM iteria_ai.frp_proposals;  EXCEPTION WHEN OTHERS THEN l_props := -1; END;

  HTP.P(''{"ok":true,"docs":''     || l_docs
       || '',"chunks":''   || l_chunks
       || '',"proposals":''|| l_props
       || '',"now":"''     || TO_CHAR(SYSTIMESTAMP, ''YYYY-MM-DD"T"HH24:MI:SS"Z"'')
       || ''"}'' );
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET library
  -- Params (all optional, all query string):
  --   :status    — deal_status filter ('won','lost','rfp','reference','all')
  --   :q         — free-text match against filename + raw_text
  --   :limit     — default 200, cap 1000
  --   :offset    — default 0
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'library',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'List FRP_DOCS for the library rail');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'library',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => 'Filter by deal_status, free-text q, paginated',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_status  VARCHAR2(40)  := LOWER(NVL(:status, ''all''));
  l_q       VARCHAR2(2000):= :q;
  l_q_lc    VARCHAR2(2000);
  l_limit   NUMBER        := NVL(TO_NUMBER(:limit),  200);
  l_offset  NUMBER        := NVL(TO_NUMBER(:offset), 0);
  l_first   BOOLEAN       := TRUE;
  l_total   NUMBER        := 0;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_limit  < 1     THEN l_limit  := 1; END IF;
  IF l_limit  > 1000  THEN l_limit  := 1000; END IF;
  IF l_offset < 0     THEN l_offset := 0; END IF;
  l_q_lc := LOWER(l_q);

  -- Total before pagination so the frontend can show "showing 9 of 623".
  SELECT COUNT(*) INTO l_total
    FROM iteria_ai.frp_docs d
   WHERE (l_status = ''all'' OR LOWER(d.deal_status) = l_status)
     AND (l_q_lc IS NULL
          OR INSTR(LOWER(d.filename), l_q_lc) > 0
          OR INSTR(LOWER(DBMS_LOB.SUBSTR(d.raw_text, 4000, 1)), l_q_lc) > 0);

  HTP.P(''{"ok":true,"total":'' || l_total
       || '',"limit":''   || l_limit
       || '',"offset":''  || l_offset
       || '',"docs":['' );

  FOR r IN (
    SELECT doc_id, filename, prefix, deal_status, size_bytes, mime_type,
           TO_CHAR(ingested_at, ''YYYY-MM-DD"T"HH24:MI:SS"Z"'') AS ingested_at
      FROM iteria_ai.frp_docs d
     WHERE (l_status = ''all'' OR LOWER(d.deal_status) = l_status)
       AND (l_q_lc IS NULL
            OR INSTR(LOWER(d.filename), l_q_lc) > 0
            OR INSTR(LOWER(DBMS_LOB.SUBSTR(d.raw_text, 4000, 1)), l_q_lc) > 0)
     ORDER BY d.ingested_at DESC NULLS LAST, d.filename
     OFFSET l_offset ROWS FETCH NEXT l_limit ROWS ONLY
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{''
       || ''"doc_id":''     || APEX_JSON.STRINGIFY(r.doc_id)
       || '',"filename":''  || APEX_JSON.STRINGIFY(NVL(r.filename,''''))
       || '',"prefix":''    || APEX_JSON.STRINGIFY(NVL(r.prefix,''''))
       || '',"deal_status":''|| APEX_JSON.STRINGIFY(NVL(r.deal_status,''''))
       || '',"size_bytes":''|| NVL(TO_CHAR(r.size_bytes), ''null'')
       || '',"mime_type":'' || APEX_JSON.STRINGIFY(NVL(r.mime_type,''''))
       || '',"ingested_at":''|| APEX_JSON.STRINGIFY(NVL(r.ingested_at,''''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET library/stats
  -- Returns counts for the library footer + per-status histogram
  -- the rail tabs use ("Won 142 · Lost 38 · RFPs 12 …").
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'library/stats',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Counts for the library footer + tab badges');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'library/stats',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_total_docs   NUMBER := 0;
  l_total_chunks NUMBER := 0;
  l_first        BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  BEGIN SELECT COUNT(*) INTO l_total_docs   FROM iteria_ai.frp_docs;   EXCEPTION WHEN OTHERS THEN l_total_docs := 0; END;
  BEGIN SELECT COUNT(*) INTO l_total_chunks FROM iteria_ai.frp_chunks; EXCEPTION WHEN OTHERS THEN l_total_chunks := 0; END;

  HTP.P(''{"ok":true,"total_docs":'' || l_total_docs
       || '',"total_chunks":''       || l_total_chunks
       || '',"by_status":['' );

  FOR r IN (
    SELECT NVL(LOWER(deal_status),''unknown'') AS deal_status,
           COUNT(*) AS n
      FROM iteria_ai.frp_docs
     GROUP BY NVL(LOWER(deal_status),''unknown'')
     ORDER BY 2 DESC
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"status":'' || APEX_JSON.STRINGIFY(r.deal_status)
       || '',"count":''  || r.n || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- POST library/scan
  -- Schedules FRP_INGEST.scan_all_prefixes as a one-shot DBMS_SCHEDULER
  -- job so the HTTP request returns immediately. The dashboard polls
  -- /library/stats to see the doc count climb.
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'library/scan',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Kick a background bucket scan');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'library/scan',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => 'Returns the scheduler job name for status polling',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_job     VARCHAR2(60)  := ''FRP_SCAN_'' || TO_CHAR(SYSTIMESTAMP, ''YYYYMMDDHH24MISSFF3'');
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  DBMS_SCHEDULER.CREATE_JOB(
    job_name        => l_job,
    job_type        => ''PLSQL_BLOCK'',
    job_action      => ''BEGIN iteria_ai.frp_ingest.scan_all_prefixes; END;'',
    start_date      => SYSTIMESTAMP,
    enabled         => TRUE,
    auto_drop       => TRUE,
    comments        => ''FRP Studio bucket scan via /frp-hooks/library/scan''
  );

  HTP.P(''{"ok":true,"job_name":"'' || l_job || ''","status":"scheduled"}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET library/:doc_id
  -- Single doc detail + chunk count. Drives the right-pane preview
  -- when the user clicks an item in the rail.
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'library/:doc_id',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Single doc detail');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'library/:doc_id',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_doc     iteria_ai.frp_docs%ROWTYPE;
  l_chunks  NUMBER := 0;
  l_meta    CLOB;
  l_preview VARCHAR2(2000);
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  BEGIN
    SELECT * INTO l_doc FROM iteria_ai.frp_docs WHERE doc_id = :doc_id;
  EXCEPTION WHEN NO_DATA_FOUND THEN
    :status := 404;
    HTP.P(''{"ok":false,"error":"doc not found"}'');
    RETURN;
  END;

  BEGIN
    SELECT COUNT(*) INTO l_chunks FROM iteria_ai.frp_chunks WHERE doc_id = l_doc.doc_id;
  EXCEPTION WHEN OTHERS THEN l_chunks := 0; END;

  IF l_doc.raw_text IS NOT NULL AND DBMS_LOB.GETLENGTH(l_doc.raw_text) > 0 THEN
    l_preview := DBMS_LOB.SUBSTR(l_doc.raw_text, 1500, 1);
  END IF;
  l_meta := l_doc.metadata_json;

  HTP.P(''{"ok":true,"doc_id":''   || APEX_JSON.STRINGIFY(l_doc.doc_id)
       || '',"filename":''         || APEX_JSON.STRINGIFY(NVL(l_doc.filename,''''))
       || '',"prefix":''           || APEX_JSON.STRINGIFY(NVL(l_doc.prefix,''''))
       || '',"deal_status":''      || APEX_JSON.STRINGIFY(NVL(l_doc.deal_status,''''))
       || '',"size_bytes":''       || NVL(TO_CHAR(l_doc.size_bytes), ''null'')
       || '',"mime_type":''        || APEX_JSON.STRINGIFY(NVL(l_doc.mime_type,''''))
       || '',"ingested_at":''      || APEX_JSON.STRINGIFY(TO_CHAR(l_doc.ingested_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || '',"chunk_count":''      || l_chunks
       || '',"preview":''          || APEX_JSON.STRINGIFY(NVL(l_preview,''''))
       || '',"metadata_json":''    || NVL(l_meta, ''null'')
       || ''}'' );
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- POST search
  -- Body: {"q":"...", "top_k":8, "deal_status":"won"}
  -- Returns top-K matching chunks with doc context + cosine score.
  -- Excludes statuses listed in FRP_CONFIG('library.exclude_status_at_match')
  -- so we don't echo our own AI drafts back as "matches".
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'search',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Vector search across FRP_CHUNKS');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'search',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => 'Top-K cosine match using FRP_INGEST.embed_one for the query',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_body    CLOB := iteria_ai.blob_to_clob_utf8(:body);
  l_q       VARCHAR2(4000);
  l_top_k   NUMBER := 8;
  l_status  VARCHAR2(40);
  l_qvec    VECTOR;
  l_first   BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_body IS NULL OR DBMS_LOB.GETLENGTH(l_body) = 0 THEN
    :status := 400; HTP.P(''{"ok":false,"error":"empty body"}''); RETURN;
  END IF;

  BEGIN APEX_JSON.PARSE(l_body); EXCEPTION WHEN OTHERS THEN
    :status := 400; HTP.P(''{"ok":false,"error":"invalid JSON"}''); RETURN;
  END;
  BEGIN l_q      := SUBSTR(APEX_JSON.GET_VARCHAR2(''q''), 1, 4000); EXCEPTION WHEN OTHERS THEN NULL; END;
  BEGIN l_top_k  := NVL(APEX_JSON.GET_NUMBER(''top_k''), 8);        EXCEPTION WHEN OTHERS THEN l_top_k := 8; END;
  BEGIN l_status := SUBSTR(APEX_JSON.GET_VARCHAR2(''deal_status''), 1, 40); EXCEPTION WHEN OTHERS THEN NULL; END;

  IF l_q IS NULL OR LENGTH(TRIM(l_q)) = 0 THEN
    :status := 400; HTP.P(''{"ok":false,"error":"q (query text) required"}''); RETURN;
  END IF;
  IF l_top_k < 1   THEN l_top_k := 1; END IF;
  IF l_top_k > 50  THEN l_top_k := 50; END IF;

  -- Reuse the same embedder the ingest pipeline uses so query and corpus
  -- live in the same vector space. embed_one is FUNCTION ... RETURN VECTOR.
  l_qvec := iteria_ai.frp_ingest.embed_one(TO_CLOB(l_q));

  HTP.P(''{"ok":true,"q":''   || APEX_JSON.STRINGIFY(l_q)
       || '',"top_k":''       || l_top_k
       || '',"matches":['' );

  FOR r IN (
    SELECT c.chunk_id, c.doc_id,
           d.filename, d.deal_status, d.prefix,
           VECTOR_DISTANCE(c.embedding, l_qvec, COSINE) AS dist,
           DBMS_LOB.SUBSTR(c.chunk_text, 1500, 1) AS preview
      FROM iteria_ai.frp_chunks c
      JOIN iteria_ai.frp_docs   d ON d.doc_id = c.doc_id
     WHERE (l_status IS NULL OR LOWER(d.deal_status) = LOWER(l_status))
       AND LOWER(NVL(d.deal_status,'''')) NOT IN (''no_bid'',''tool_output'')
     ORDER BY VECTOR_DISTANCE(c.embedding, l_qvec, COSINE)
     FETCH FIRST l_top_k ROWS ONLY
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"chunk_id":''      || APEX_JSON.STRINGIFY(r.chunk_id)
       || '',"doc_id":''        || APEX_JSON.STRINGIFY(r.doc_id)
       || '',"filename":''      || APEX_JSON.STRINGIFY(NVL(r.filename,''''))
       || '',"deal_status":''   || APEX_JSON.STRINGIFY(NVL(r.deal_status,''''))
       || '',"prefix":''        || APEX_JSON.STRINGIFY(NVL(r.prefix,''''))
       || '',"score":''         || TO_CHAR(ROUND(1 - r.dist, 4))
       || '',"preview":''       || APEX_JSON.STRINGIFY(NVL(r.preview,''''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET proposals
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'proposals',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'List + create proposals');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'proposals',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => 'List recent proposals',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_limit   NUMBER := NVL(TO_NUMBER(:limit), 100);
  l_first   BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_limit < 1   THEN l_limit := 1; END IF;
  IF l_limit > 500 THEN l_limit := 500; END IF;

  HTP.P(''{"ok":true,"proposals":['' );
  FOR r IN (
    SELECT proposal_id, client_name, status,
           TO_CHAR(created_at, ''YYYY-MM-DD"T"HH24:MI:SS"Z"'') AS created_at,
           TO_CHAR(updated_at, ''YYYY-MM-DD"T"HH24:MI:SS"Z"'') AS updated_at,
           rfp_doc_id
      FROM iteria_ai.frp_proposals
     ORDER BY updated_at DESC NULLS LAST, created_at DESC
     FETCH FIRST l_limit ROWS ONLY
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"proposal_id":'' || APEX_JSON.STRINGIFY(r.proposal_id)
       || '',"client_name":'' || APEX_JSON.STRINGIFY(NVL(r.client_name,''''))
       || '',"status":''      || APEX_JSON.STRINGIFY(NVL(r.status,''draft''))
       || '',"rfp_doc_id":''  || APEX_JSON.STRINGIFY(NVL(r.rfp_doc_id,''''))
       || '',"created_at":''  || APEX_JSON.STRINGIFY(NVL(r.created_at,''''))
       || '',"updated_at":''  || APEX_JSON.STRINGIFY(NVL(r.updated_at,''''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'proposals',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => 'Create a draft proposal — returns proposal_id',
      p_source        =>
'DECLARE
  l_api_key   VARCHAR2(200);
  l_req_key   VARCHAR2(200) := :api_key;
  l_body      CLOB := iteria_ai.blob_to_clob_utf8(:body);
  l_client    VARCHAR2(500);
  l_rfp_doc   VARCHAR2(64);
  l_due_iso   VARCHAR2(40);
  l_prop_id   VARCHAR2(64);
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_body IS NOT NULL AND DBMS_LOB.GETLENGTH(l_body) > 0 THEN
    BEGIN APEX_JSON.PARSE(l_body); EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN l_client  := SUBSTR(APEX_JSON.GET_VARCHAR2(''client_name''), 1, 500); EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN l_rfp_doc := SUBSTR(APEX_JSON.GET_VARCHAR2(''rfp_doc_id''),  1, 64);  EXCEPTION WHEN OTHERS THEN NULL; END;
    BEGIN l_due_iso := SUBSTR(APEX_JSON.GET_VARCHAR2(''due_date''),    1, 40);  EXCEPTION WHEN OTHERS THEN NULL; END;
  END IF;

  l_prop_id := LOWER(SYS_GUID());

  INSERT INTO iteria_ai.frp_proposals
    (proposal_id, client_name, rfp_doc_id, status, created_at, updated_at)
  VALUES
    (l_prop_id, l_client, l_rfp_doc, ''draft'', SYSTIMESTAMP, SYSTIMESTAMP);

  COMMIT;
  HTP.P(''{"ok":true,"proposal_id":''|| APEX_JSON.STRINGIFY(l_prop_id) || ''}'');
EXCEPTION WHEN OTHERS THEN
  ROLLBACK;
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET proposals/:id
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'proposals/:id',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'One proposal — full state');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'proposals/:id',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_p       iteria_ai.frp_proposals%ROWTYPE;
  l_first   BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  BEGIN
    SELECT * INTO l_p FROM iteria_ai.frp_proposals WHERE proposal_id = :id;
  EXCEPTION WHEN NO_DATA_FOUND THEN
    :status := 404; HTP.P(''{"ok":false,"error":"proposal not found"}''); RETURN;
  END;

  HTP.P(''{"ok":true,"proposal_id":'' || APEX_JSON.STRINGIFY(l_p.proposal_id)
       || '',"client_name":''         || APEX_JSON.STRINGIFY(NVL(l_p.client_name,''''))
       || '',"status":''              || APEX_JSON.STRINGIFY(NVL(l_p.status,''draft''))
       || '',"rfp_doc_id":''          || APEX_JSON.STRINGIFY(NVL(l_p.rfp_doc_id,''''))
       || '',"created_at":''          || APEX_JSON.STRINGIFY(TO_CHAR(l_p.created_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || '',"updated_at":''          || APEX_JSON.STRINGIFY(TO_CHAR(l_p.updated_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || '',"extracted_json":''      || NVL(l_p.extracted_json, ''null'')
       || '',"draft_text":''          || APEX_JSON.STRINGIFY(NVL(DBMS_LOB.SUBSTR(l_p.draft_text, 32000, 1),''''))
       || '',"attachments":['' );

  FOR r IN (
    SELECT a.doc_id, a.attach_role, d.filename, d.deal_status,
           TO_CHAR(a.attached_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"'') AS attached_at
      FROM iteria_ai.frp_proposal_attachments a
      JOIN iteria_ai.frp_docs d ON d.doc_id = a.doc_id
     WHERE a.proposal_id = l_p.proposal_id
     ORDER BY a.attached_at
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"doc_id":''      || APEX_JSON.STRINGIFY(r.doc_id)
       || '',"role":''        || APEX_JSON.STRINGIFY(NVL(r.attach_role,''reference''))
       || '',"filename":''    || APEX_JSON.STRINGIFY(NVL(r.filename,''''))
       || '',"deal_status":'' || APEX_JSON.STRINGIFY(NVL(r.deal_status,''''))
       || '',"attached_at":'' || APEX_JSON.STRINGIFY(NVL(r.attached_at,''''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- POST proposals/:id/generate
  -- Queues FRP_PIPELINE.run_proposal in the background. Returns
  -- the scheduler job name so the UI can poll /proposals/:id and
  -- watch status/draft_text update.
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'proposals/:id/generate',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Kick the RFP→match→draft pipeline');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'proposals/:id/generate',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => 'Marks proposal generating + schedules FRP_PIPELINE.run_proposal',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_id      VARCHAR2(64)  := :id;
  l_job     VARCHAR2(60);
  l_exists  NUMBER := 0;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  SELECT COUNT(*) INTO l_exists FROM iteria_ai.frp_proposals WHERE proposal_id = l_id;
  IF l_exists = 0 THEN
    :status := 404; HTP.P(''{"ok":false,"error":"proposal not found"}''); RETURN;
  END IF;

  UPDATE iteria_ai.frp_proposals
     SET status = ''generating'', updated_at = SYSTIMESTAMP
   WHERE proposal_id = l_id;

  l_job := ''FRP_GEN_'' || SUBSTR(REPLACE(l_id,''-'',''''), 1, 16) || ''_'' || TO_CHAR(SYSTIMESTAMP,''SSXFF3'');

  DBMS_SCHEDULER.CREATE_JOB(
    job_name        => l_job,
    job_type        => ''PLSQL_BLOCK'',
    job_action      => ''BEGIN iteria_ai.frp_pipeline.run_proposal('''''' || l_id || ''''''); END;'',
    start_date      => SYSTIMESTAMP,
    enabled         => TRUE,
    auto_drop       => TRUE,
    comments        => ''FRP Studio pipeline run for '' || l_id
  );

  COMMIT;
  HTP.P(''{"ok":true,"proposal_id":''|| APEX_JSON.STRINGIFY(l_id)
       || '',"job_name":''           || APEX_JSON.STRINGIFY(l_job)
       || '',"status":"generating"}'' );
EXCEPTION WHEN OTHERS THEN
  ROLLBACK;
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- POST proposals/:id/attachments
  -- Body: {"doc_id":"...", "role":"reference"}
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'proposals/:id/attachments',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Attach a library doc to a proposal');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'proposals/:id/attachments',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_id      VARCHAR2(64)  := :id;
  l_body    CLOB          := iteria_ai.blob_to_clob_utf8(:body);
  l_doc_id  VARCHAR2(64);
  l_role    VARCHAR2(32) := ''reference'';
  l_attach  VARCHAR2(64) := LOWER(SYS_GUID());
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_body IS NULL OR DBMS_LOB.GETLENGTH(l_body) = 0 THEN
    :status := 400; HTP.P(''{"ok":false,"error":"empty body"}''); RETURN;
  END IF;
  BEGIN APEX_JSON.PARSE(l_body); EXCEPTION WHEN OTHERS THEN
    :status := 400; HTP.P(''{"ok":false,"error":"invalid JSON"}''); RETURN;
  END;
  BEGIN l_doc_id := SUBSTR(APEX_JSON.GET_VARCHAR2(''doc_id''), 1, 64); EXCEPTION WHEN OTHERS THEN NULL; END;
  BEGIN l_role   := NVL(SUBSTR(APEX_JSON.GET_VARCHAR2(''role''), 1, 32), ''reference''); EXCEPTION WHEN OTHERS THEN l_role := ''reference''; END;

  IF l_doc_id IS NULL THEN
    :status := 400; HTP.P(''{"ok":false,"error":"doc_id required"}''); RETURN;
  END IF;

  BEGIN
    INSERT INTO iteria_ai.frp_proposal_attachments
      (attach_id, proposal_id, doc_id, attach_role, attached_at)
    VALUES
      (l_attach, l_id, l_doc_id, l_role, SYSTIMESTAMP);
  EXCEPTION WHEN DUP_VAL_ON_INDEX THEN
    -- Already attached with this role; treat as success.
    NULL;
  END;

  UPDATE iteria_ai.frp_proposals SET updated_at = SYSTIMESTAMP WHERE proposal_id = l_id;

  COMMIT;
  HTP.P(''{"ok":true,"proposal_id":''|| APEX_JSON.STRINGIFY(l_id)
       || '',"doc_id":''             || APEX_JSON.STRINGIFY(l_doc_id)
       || '',"role":''               || APEX_JSON.STRINGIFY(l_role)
       || ''}'' );
EXCEPTION WHEN OTHERS THEN
  ROLLBACK;
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET bucket/folders
  -- Histogram of FRP_DOCS by prefix (one row per OCI bucket folder).
  -- Powers the "Connect / Ingest" admin page.
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'bucket/folders',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Doc count + bytes per bucket prefix');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'bucket/folders',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => NULL,
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_first   BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  HTP.P(''{"ok":true,"folders":['' );
  FOR r IN (
    SELECT NVL(prefix,''(none)'') AS prefix,
           COUNT(*)                AS doc_count,
           SUM(NVL(size_bytes,0))  AS total_bytes,
           MAX(ingested_at)        AS latest
      FROM iteria_ai.frp_docs
     GROUP BY NVL(prefix,''(none)'')
     ORDER BY doc_count DESC
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"prefix":''     || APEX_JSON.STRINGIFY(r.prefix)
       || '',"doc_count":''  || r.doc_count
       || '',"total_bytes":''|| NVL(TO_CHAR(r.total_bytes), ''0'')
       || '',"latest":''     || APEX_JSON.STRINGIFY(TO_CHAR(r.latest,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

COMMIT;

EXCEPTION
  WHEN OTHERS THEN
    ROLLBACK;
    RAISE;
END;
/

-- ─────────────────────────────────────────────────────────
-- Smoke test (run after the above completes):
--
--   SELECT module_name, base_path, status FROM user_ords_modules WHERE module_name='frp-hooks';
--   SELECT pattern, p.method FROM user_ords_templates t JOIN user_ords_handlers p ON p.template_id=t.id
--     WHERE t.module_name='frp-hooks' ORDER BY pattern, p.method;
--
-- Then in a browser, hitting (substitute your ords URL + api_key):
--   /ords/admin/frp-hooks/health?api_key=...
-- should return: {"ok":true,"docs":555,"chunks":...,"proposals":0,"now":"..."}
-- ─────────────────────────────────────────────────────────
