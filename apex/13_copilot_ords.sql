-- ═══════════════════════════════════════════════════════════════════
-- C4 — Studio Copilot ORDS endpoints
-- Three handlers added to the existing frp-hooks module:
--   POST /copilot/ask                      — send a message, get a reply
--   GET  /copilot/conversations            — list conversations for a user
--   GET  /copilot/conversations/:id/messages — load history for one conv
--
-- Same api_key pattern as the rest of the module.
-- Idempotent — ORDS.DEFINE_HANDLER replaces existing handlers in place.
-- Run as ITERIA_AI in Database Actions → SQL.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

BEGIN

  -- ─────────────────────────────────────────────────────────
  -- POST /copilot/ask
  -- Body: {user_id, message, form_state?, proposal_id?, conversation_id?}
  -- Returns: {ok, conversation_id, answer, input_tokens, output_tokens,
  --          provider, model, elapsed_ms}
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'copilot/ask',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Studio Copilot — send a message');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'copilot/ask',
      p_method        => 'POST',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => 'application/json',
      p_comments      => 'Calls FRP_COPILOT.ask, returns its JSON envelope verbatim',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_body    CLOB := :body_text;
  l_user    VARCHAR2(256);
  l_msg     CLOB;
  l_form    CLOB;
  l_prop    VARCHAR2(64);
  l_conv    VARCHAR2(64);
  l_result  CLOB;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  l_user := JSON_VALUE(l_body, ''$.user_id'');
  l_msg  := JSON_VALUE(l_body, ''$.message'' RETURNING CLOB);
  l_form := JSON_QUERY(l_body, ''$.form_state'' RETURNING CLOB);
  l_prop := JSON_VALUE(l_body, ''$.proposal_id'');
  l_conv := JSON_VALUE(l_body, ''$.conversation_id'');

  IF l_user IS NULL THEN
    :status := 400; HTP.P(''{"ok":false,"error":"user_id required"}''); RETURN;
  END IF;
  IF l_msg IS NULL OR DBMS_LOB.GETLENGTH(l_msg) = 0 THEN
    :status := 400; HTP.P(''{"ok":false,"error":"message required"}''); RETURN;
  END IF;

  l_result := iteria_ai.frp_copilot.ask(
    p_user_id         => l_user,
    p_message         => l_msg,
    p_form_state      => l_form,
    p_proposal_id     => l_prop,
    p_conversation_id => l_conv);

  HTP.P(l_result);
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,500),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET /copilot/conversations?user_id=...&proposal_id=...&limit=...
  -- Returns: {ok, conversations:[{conversation_id, title, message_count,
  --          proposal_id, created_at, updated_at, last_message_preview}]}
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'copilot/conversations',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'List a user''s copilot conversations');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'copilot/conversations',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => 'Returns conversations matching ?user_id=&proposal_id=',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_user    VARCHAR2(256) := :user_id;
  l_prop    VARCHAR2(64)  := :proposal_id;
  l_limit   NUMBER        := NVL(TO_NUMBER(:limit_), 50);
  l_first   BOOLEAN       := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  IF l_user IS NULL THEN
    :status := 400; HTP.P(''{"ok":false,"error":"user_id query param required"}''); RETURN;
  END IF;

  HTP.P(''{"ok":true,"conversations":['');
  FOR rec IN (
    SELECT c.conversation_id, c.title, c.message_count,
           c.proposal_id, c.created_at, c.updated_at,
           (SELECT DBMS_LOB.SUBSTR(content, 200, 1)
              FROM (SELECT content FROM iteria_ai.frp_copilot_messages
                     WHERE conversation_id = c.conversation_id
                       AND role IN (''user'',''assistant'')
                     ORDER BY created_at DESC FETCH FIRST 1 ROWS ONLY))
              AS last_preview
      FROM iteria_ai.frp_copilot_conversations c
     WHERE c.user_id = l_user
       AND (l_prop IS NULL OR c.proposal_id = l_prop)
     ORDER BY c.updated_at DESC NULLS LAST
     FETCH FIRST l_limit ROWS ONLY
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"conversation_id":'' || APEX_JSON.STRINGIFY(rec.conversation_id)
       || '',"title":''            || APEX_JSON.STRINGIFY(NVL(rec.title,''''))
       || '',"message_count":''    || NVL(TO_CHAR(rec.message_count), ''0'')
       || '',"proposal_id":''      || APEX_JSON.STRINGIFY(NVL(rec.proposal_id,''''))
       || '',"created_at":''       || APEX_JSON.STRINGIFY(TO_CHAR(rec.created_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || '',"updated_at":''       || APEX_JSON.STRINGIFY(TO_CHAR(rec.updated_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || '',"last_preview":''     || APEX_JSON.STRINGIFY(NVL(rec.last_preview,''''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  -- ─────────────────────────────────────────────────────────
  -- GET /copilot/conversations/:conv_id/messages
  -- Returns: {ok, conversation:{...}, messages:[{role, content,
  --          citations, tool_calls, applied, created_at}]}
  -- ─────────────────────────────────────────────────────────
  ORDS.DEFINE_TEMPLATE(
      p_module_name => 'frp-hooks',
      p_pattern     => 'copilot/conversations/:conv_id/messages',
      p_priority    => 0,
      p_etag_type   => 'NONE',
      p_comments    => 'Load message history for one conversation');

  ORDS.DEFINE_HANDLER(
      p_module_name   => 'frp-hooks',
      p_pattern       => 'copilot/conversations/:conv_id/messages',
      p_method        => 'GET',
      p_source_type   => 'plsql/block',
      p_mimes_allowed => NULL,
      p_comments      => 'Chronological history; user/assistant only by default',
      p_source        =>
'DECLARE
  l_api_key VARCHAR2(200);
  l_req_key VARCHAR2(200) := :api_key;
  l_conv   VARCHAR2(64);
  l_user_id VARCHAR2(256);
  l_proposal_id VARCHAR2(64);
  l_title  VARCHAR2(200);
  l_count  NUMBER;
  l_gist   CLOB;
  l_first  BOOLEAN := TRUE;
BEGIN
  BEGIN SELECT api_key INTO l_api_key FROM iteria_ai.api_configuration
        WHERE is_active=''Y'' AND ROWNUM=1;
  EXCEPTION WHEN NO_DATA_FOUND THEN l_api_key := NULL; END;
  IF l_api_key IS NOT NULL AND NVL(l_req_key,''__none__'') != l_api_key THEN
    :status := 403; HTP.P(''{"ok":false,"error":"unauthorized"}''); RETURN;
  END IF;

  l_conv := :conv_id;

  BEGIN
    SELECT user_id, proposal_id, title, message_count, gist
      INTO l_user_id, l_proposal_id, l_title, l_count, l_gist
      FROM iteria_ai.frp_copilot_conversations
     WHERE conversation_id = l_conv;
  EXCEPTION WHEN NO_DATA_FOUND THEN
    :status := 404; HTP.P(''{"ok":false,"error":"conversation not found"}''); RETURN;
  END;

  HTP.P(''{"ok":true,"conversation":{''
     || ''"conversation_id":'' || APEX_JSON.STRINGIFY(l_conv)
     || '',"user_id":''       || APEX_JSON.STRINGIFY(l_user_id)
     || '',"proposal_id":''   || APEX_JSON.STRINGIFY(NVL(l_proposal_id,''''))
     || '',"title":''         || APEX_JSON.STRINGIFY(NVL(l_title,''''))
     || '',"message_count":'' || NVL(TO_CHAR(l_count), ''0'')
     || '',"has_gist":''      || CASE WHEN l_gist IS NOT NULL AND DBMS_LOB.GETLENGTH(l_gist) > 0 THEN ''true'' ELSE ''false'' END
     || ''},"messages":['');

  FOR rec IN (
    SELECT message_id, role, content, citations_json, tool_calls_json,
           applied_json, created_at
      FROM iteria_ai.frp_copilot_messages
     WHERE conversation_id = l_conv
       AND role IN (''user'',''assistant'')
     ORDER BY created_at
  ) LOOP
    IF NOT l_first THEN HTP.P('',''); END IF;
    l_first := FALSE;
    HTP.P(''{"message_id":''      || APEX_JSON.STRINGIFY(rec.message_id)
       || '',"role":''            || APEX_JSON.STRINGIFY(rec.role)
       || '',"content":''         || APEX_JSON.STRINGIFY(NVL(DBMS_LOB.SUBSTR(rec.content, 8000, 1),''''))
       || '',"citations":''       || NVL(rec.citations_json, ''[]'')
       || '',"tool_calls":''      || NVL(rec.tool_calls_json, ''[]'')
       || '',"applied":''         || NVL(rec.applied_json, ''[]'')
       || '',"created_at":''      || APEX_JSON.STRINGIFY(TO_CHAR(rec.created_at,''YYYY-MM-DD"T"HH24:MI:SS"Z"''))
       || ''}'' );
  END LOOP;
  HTP.P('']}'');
EXCEPTION WHEN OTHERS THEN
  :status := 500;
  HTP.P(''{"ok":false,"error":"'' || REPLACE(SUBSTR(SQLERRM,1,300),''"'',''`'') || ''"}'');
END;');

  COMMIT;
END;
/

-- Verify
SELECT pattern, method
  FROM (
    SELECT t.uri_template AS pattern, h.method
      FROM user_ords_modules m
      JOIN user_ords_templates t ON t.module_id = m.id
      JOIN user_ords_handlers  h ON h.template_id = t.id
     WHERE m.name = 'frp-hooks'
       AND t.uri_template LIKE 'copilot%'
  )
 ORDER BY pattern, method;
