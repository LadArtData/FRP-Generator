-- ═══════════════════════════════════════════════════════════════════
-- FRP_COPILOT — Studio Copilot core
-- C1 of STUDIO_COPILOT_SPEC.md.
--
-- First cut: Grok-only via OCI, persists conversations, loads last 10
-- turns of history, retrieves top-8 library chunks per question.
-- NOT YET: gist generation (C1b), tool calls (C3), Claude override (C6).
--
-- Run as ITERIA_AI in Database Actions → SQL.
-- Spec then body, separated by /. Run with F5 (Run Script).
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE FRP_COPILOT AS

  -- Main entry. Returns JSON CLOB:
  --   {ok, conversation_id, answer, citations[], input_tokens,
  --    output_tokens, provider, model, elapsed_ms}
  FUNCTION ask(
    p_user_id         IN VARCHAR2,
    p_message         IN CLOB,
    p_form_state      IN CLOB     DEFAULT NULL,
    p_proposal_id     IN VARCHAR2 DEFAULT NULL,
    p_conversation_id IN VARCHAR2 DEFAULT NULL
  ) RETURN CLOB;

END FRP_COPILOT;
/

SHOW ERRORS PACKAGE FRP_COPILOT;


CREATE OR REPLACE PACKAGE BODY FRP_COPILOT AS

  C_GENAI_CHAT_URL CONSTANT VARCHAR2(256) :=
    'https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/20231130/actions/chat';
  C_OCI_COMPARTMENT_ID CONSTANT VARCHAR2(256) :=
    'ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2japj37aoagj6kcbfoa';
  C_GROK_OCID CONSTANT VARCHAR2(256) :=
    'ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceya4fxp5zjj27q24rjxk46l43die7u6nclgwfbemklsdvoa';
  C_TOP_K_CHUNKS CONSTANT NUMBER := 8;

  -- ─────────────────────────────────────────────────────────
  -- Find existing conversation by (user, proposal) or by explicit id;
  -- create a new one if neither matches.
  -- ─────────────────────────────────────────────────────────
  FUNCTION ensure_conversation(
    p_user_id         IN VARCHAR2,
    p_proposal_id     IN VARCHAR2,
    p_conversation_id IN VARCHAR2
  ) RETURN VARCHAR2 IS
    l_id VARCHAR2(64);
  BEGIN
    IF p_conversation_id IS NOT NULL THEN
      BEGIN
        SELECT conversation_id INTO l_id
          FROM FRP_COPILOT_CONVERSATIONS
         WHERE conversation_id = p_conversation_id
           AND user_id = p_user_id;
        RETURN l_id;
      EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
      END;
    END IF;

    -- Find existing (user_id, proposal_id) row. Has to handle NULL
    -- proposal_id explicitly because '= NULL' never matches in SQL.
    BEGIN
      SELECT conversation_id INTO l_id
        FROM FRP_COPILOT_CONVERSATIONS
       WHERE user_id = p_user_id
         AND ((p_proposal_id IS NOT NULL AND proposal_id = p_proposal_id)
           OR (p_proposal_id IS NULL     AND proposal_id IS NULL));
      RETURN l_id;
    EXCEPTION WHEN NO_DATA_FOUND THEN NULL;
    END;

    INSERT INTO FRP_COPILOT_CONVERSATIONS(user_id, proposal_id)
      VALUES (p_user_id, p_proposal_id)
    RETURNING conversation_id INTO l_id;
    RETURN l_id;
  END ensure_conversation;

  -- ─────────────────────────────────────────────────────────
  PROCEDURE save_message(
    p_conv_id        IN VARCHAR2,
    p_role           IN VARCHAR2,
    p_content        IN CLOB,
    p_tool_calls_json IN CLOB    DEFAULT NULL,
    p_input_tokens   IN NUMBER   DEFAULT NULL,
    p_output_tokens  IN NUMBER   DEFAULT NULL,
    p_provider       IN VARCHAR2 DEFAULT NULL,
    p_model          IN VARCHAR2 DEFAULT NULL
  ) IS
  BEGIN
    INSERT INTO FRP_COPILOT_MESSAGES
      (conversation_id, role, content, tool_calls_json,
       input_tokens, output_tokens, provider, model)
    VALUES
      (p_conv_id, p_role, p_content, p_tool_calls_json,
       p_input_tokens, p_output_tokens, p_provider, p_model);

    UPDATE FRP_COPILOT_CONVERSATIONS
       SET message_count = message_count + 1,
           updated_at    = CURRENT_TIMESTAMP
     WHERE conversation_id = p_conv_id;
  END save_message;

  -- ─────────────────────────────────────────────────────────
  -- Extract <tool_call>{...}</tool_call> blocks from the model's
  -- response. Returns a JSON array of {index, tool, args} objects;
  -- the markers are stripped from p_text in place (so the answer
  -- displayed to the user doesn't have raw JSON garbage in it).
  -- ─────────────────────────────────────────────────────────
  PROCEDURE parse_tool_calls(
    p_text       IN OUT NOCOPY CLOB,
    p_tool_calls IN OUT NOCOPY JSON_ARRAY_T
  ) IS
    l_match_count NUMBER := 0;
    l_match       VARCHAR2(8000);
    l_inner       VARCHAR2(8000);
    l_idx         NUMBER := 0;
    j_tc          JSON_OBJECT_T;
    j_parsed      JSON_OBJECT_T;
  BEGIN
    p_tool_calls := JSON_ARRAY_T();

    -- Iteratively find each <tool_call>...</tool_call>; both extract
    -- and strip from the running text.
    LOOP
      l_match := REGEXP_SUBSTR(p_text, '<tool_call>(.*?)</tool_call>', 1, 1, 'n');
      EXIT WHEN l_match IS NULL OR l_match_count > 20;
      l_match_count := l_match_count + 1;
      l_inner := REGEXP_REPLACE(l_match, '<tool_call>(.*?)</tool_call>', '\1', 1, 1, 'n');
      BEGIN
        j_parsed := JSON_OBJECT_T.parse(l_inner);
        j_tc := JSON_OBJECT_T();
        j_tc.put('index', l_idx);
        j_tc.put('tool',  j_parsed.get_string('tool'));
        IF j_parsed.has('args') THEN
          j_tc.put('args', j_parsed.get_object('args'));
        ELSE
          j_tc.put('args', JSON_OBJECT_T());
        END IF;
        p_tool_calls.append(j_tc);
        l_idx := l_idx + 1;
      EXCEPTION WHEN OTHERS THEN
        -- Malformed JSON inside the tag — skip it but still strip
        NULL;
      END;
      p_text := REGEXP_REPLACE(p_text, '<tool_call>(.*?)</tool_call>', '', 1, 1, 'n');
    END LOOP;

    -- Tidy up the resulting answer text
    p_text := REGEXP_REPLACE(p_text, '\n{3,}', CHR(10) || CHR(10));
    p_text := TRIM(p_text);
  END parse_tool_calls;

  -- ─────────────────────────────────────────────────────────
  -- Rolling gist — every 10 messages, ask Grok to summarize the
  -- conversation in ~200 words and save it to FRP_COPILOT_CONVERSATIONS.gist.
  -- The gist gets injected into future ask() calls so older context
  -- survives the 10-turn history window.
  -- ─────────────────────────────────────────────────────────
  PROCEDURE maybe_update_gist(p_conv_id IN VARCHAR2) IS
    l_count       NUMBER;
    l_transcript  CLOB;
    l_line        VARCHAR2(8000);
    l_resp        DBMS_CLOUD_TYPES.RESP;
    l_body        VARCHAR2(32767);
    l_resp_txt    CLOB;
    l_status      NUMBER;
    l_summary     CLOB;
    l_last_msg_id VARCHAR2(64);

    j_root      JSON_OBJECT_T := JSON_OBJECT_T();
    j_serving   JSON_OBJECT_T := JSON_OBJECT_T();
    j_chat      JSON_OBJECT_T := JSON_OBJECT_T();
    j_messages  JSON_ARRAY_T  := JSON_ARRAY_T();
    j_msg       JSON_OBJECT_T;
    j_content   JSON_ARRAY_T;
    j_part      JSON_OBJECT_T;
  BEGIN
    SELECT message_count INTO l_count
      FROM FRP_COPILOT_CONVERSATIONS WHERE conversation_id = p_conv_id;

    -- Trigger every 10 messages; first eligible point is count = 10
    IF l_count < 10 OR MOD(l_count, 10) != 0 THEN RETURN; END IF;

    -- Build transcript from all user/assistant turns
    DBMS_LOB.CREATETEMPORARY(l_transcript, TRUE);
    FOR rec IN (
      SELECT role, DBMS_LOB.SUBSTR(content, 2000, 1) AS preview
        FROM FRP_COPILOT_MESSAGES
       WHERE conversation_id = p_conv_id
         AND role IN ('user','assistant')
       ORDER BY created_at
    ) LOOP
      l_line := UPPER(rec.role) || ': ' || rec.preview || CHR(10) || CHR(10);
      DBMS_LOB.WRITEAPPEND(l_transcript, LENGTH(l_line), l_line);
    END LOOP;

    -- Latest message id (anchor for the next gist trigger)
    SELECT message_id INTO l_last_msg_id
      FROM (SELECT message_id FROM FRP_COPILOT_MESSAGES
             WHERE conversation_id = p_conv_id
             ORDER BY created_at DESC FETCH FIRST 1 ROWS ONLY);

    -- Ask Grok to summarize
    j_part := JSON_OBJECT_T();
    j_part.put('type', 'TEXT');
    j_part.put('text',
      'Summarize the following FRP Studio assistant conversation in ' ||
      'about 200 words. Focus on: key facts about the client, decisions ' ||
      'made, action items still open, and anything the user might want ' ||
      'to recall later. Write it as a third-person note (e.g., ' ||
      '"User asked about X. Assistant explained Y."). No preamble, just ' ||
      'the summary.' || CHR(10) || CHR(10) || 'CONVERSATION:' || CHR(10) ||
      DBMS_LOB.SUBSTR(l_transcript, 24000, 1));
    j_content := JSON_ARRAY_T();
    j_content.append(j_part);
    j_msg := JSON_OBJECT_T();
    j_msg.put('role', 'USER');
    j_msg.put('content', j_content);
    j_messages.append(j_msg);

    j_chat.put('apiFormat',   'GENERIC');
    j_chat.put('messages',    j_messages);
    j_chat.put('maxTokens',   400);
    j_chat.put('temperature', 0.2);
    j_chat.put('topP',        0.9);
    j_chat.put('isStream',    FALSE);

    j_serving.put('servingType', 'ON_DEMAND');
    j_serving.put('modelId',     C_GROK_OCID);

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
    l_status   := DBMS_CLOUD.GET_RESPONSE_STATUS_CODE(l_resp);
    l_resp_txt := DBMS_CLOUD.GET_RESPONSE_TEXT(l_resp);

    IF l_status = 200 THEN
      l_summary := JSON_VALUE(l_resp_txt,
        '$.chatResponse.choices[0].message.content[0].text' RETURNING CLOB);
      UPDATE FRP_COPILOT_CONVERSATIONS
         SET gist                   = l_summary,
             gist_anchor_message_id = l_last_msg_id,
             updated_at             = CURRENT_TIMESTAMP
       WHERE conversation_id = p_conv_id;
    END IF;

    DBMS_LOB.FREETEMPORARY(l_transcript);
  EXCEPTION
    WHEN OTHERS THEN NULL;  -- gist failure is non-fatal
  END maybe_update_gist;

  -- ─────────────────────────────────────────────────────────
  -- Top-K library chunks most relevant to the user's question.
  -- Returns a CLOB block formatted as repeated:
  --   [doc_id=...|file=...|status=...]
  --   <chunk excerpt up to 800 chars>
  --   ---
  -- Suitable for direct injection into the prompt.
  -- ─────────────────────────────────────────────────────────
  FUNCTION retrieve_context(p_message IN CLOB) RETURN CLOB IS
    l_qvec   VECTOR;
    l_ctx    CLOB;
    l_block  VARCHAR2(2000);
  BEGIN
    l_qvec := iteria_ai.frp_ingest.embed_one(p_message);
    DBMS_LOB.CREATETEMPORARY(l_ctx, TRUE);

    FOR rec IN (
      SELECT chunk_id, doc_id, filename, deal_status, chunk_preview
        FROM (
          SELECT c.chunk_id, c.doc_id, d.filename, d.deal_status,
                 DBMS_LOB.SUBSTR(c.chunk_text, 800, 1) AS chunk_preview,
                 VECTOR_DISTANCE(c.embedding, l_qvec, COSINE) AS dist
            FROM iteria_ai.frp_chunks c
            JOIN iteria_ai.frp_docs   d ON d.doc_id = c.doc_id
           WHERE d.deal_status NOT IN ('no_bid','tool_output')
           ORDER BY 6
        )
       WHERE ROWNUM <= C_TOP_K_CHUNKS
    ) LOOP
      l_block := '[doc_id=' || rec.doc_id ||
                 '|file='   || SUBSTR(rec.filename, 1, 200) ||
                 '|status=' || rec.deal_status || ']' || CHR(10);
      DBMS_LOB.WRITEAPPEND(l_ctx, LENGTH(l_block), l_block);
      DBMS_LOB.WRITEAPPEND(l_ctx, LENGTH(rec.chunk_preview), rec.chunk_preview);
      DBMS_LOB.WRITEAPPEND(l_ctx, 5, CHR(10) || '---' || CHR(10));
    END LOOP;

    RETURN l_ctx;
  END retrieve_context;

  -- ─────────────────────────────────────────────────────────
  -- Last 10 user/assistant turns in chronological order, as a
  -- JSON_ARRAY_T of {role, content}.
  -- ─────────────────────────────────────────────────────────
  FUNCTION load_history(p_conv_id IN VARCHAR2) RETURN JSON_ARRAY_T IS
    j_hist JSON_ARRAY_T := JSON_ARRAY_T();
    j_msg  JSON_OBJECT_T;
  BEGIN
    FOR rec IN (
      SELECT role, content
        FROM (
          SELECT role, content, created_at
            FROM FRP_COPILOT_MESSAGES
           WHERE conversation_id = p_conv_id
             AND role IN ('user','assistant')
           ORDER BY created_at DESC
           FETCH FIRST 10 ROWS ONLY
        )
       ORDER BY created_at
    ) LOOP
      j_msg := JSON_OBJECT_T();
      j_msg.put('role', LOWER(rec.role));
      j_msg.put('content', DBMS_LOB.SUBSTR(rec.content, 4000, 1));
      j_hist.append(j_msg);
    END LOOP;
    RETURN j_hist;
  END load_history;

  -- ─────────────────────────────────────────────────────────
  -- Main entry
  -- ─────────────────────────────────────────────────────────
  FUNCTION ask(
    p_user_id         IN VARCHAR2,
    p_message         IN CLOB,
    p_form_state      IN CLOB     DEFAULT NULL,
    p_proposal_id     IN VARCHAR2 DEFAULT NULL,
    p_conversation_id IN VARCHAR2 DEFAULT NULL
  ) RETURN CLOB IS
    l_conv_id VARCHAR2(64);
    l_gist    CLOB;
    l_context CLOB;
    l_history JSON_ARRAY_T;
    l_started TIMESTAMP := SYSTIMESTAMP;

    l_resp     DBMS_CLOUD_TYPES.RESP;
    l_body     VARCHAR2(32767);
    l_resp_txt CLOB;
    l_status   NUMBER;
    l_answer   CLOB;
    l_in_tok   NUMBER;
    l_out_tok  NUMBER;
    l_elapsed  NUMBER;

    l_system_prompt VARCHAR2(2000);
    l_user_prompt   VARCHAR2(28000);

    j_root      JSON_OBJECT_T := JSON_OBJECT_T();
    j_serving   JSON_OBJECT_T := JSON_OBJECT_T();
    j_chat      JSON_OBJECT_T := JSON_OBJECT_T();
    j_messages  JSON_ARRAY_T  := JSON_ARRAY_T();
    j_msg       JSON_OBJECT_T;
    j_content   JSON_ARRAY_T;
    j_part      JSON_OBJECT_T;
    j_response  JSON_OBJECT_T;
  BEGIN
    -- 1. Find/create conversation, save user message
    l_conv_id := ensure_conversation(p_user_id, p_proposal_id, p_conversation_id);
    save_message(l_conv_id, 'user', p_message);
    -- 2. Get gist (rolling summary, written every 10th turn — added later)
    BEGIN
      SELECT gist INTO l_gist
        FROM FRP_COPILOT_CONVERSATIONS
       WHERE conversation_id = l_conv_id;
    EXCEPTION WHEN OTHERS THEN l_gist := NULL; END;

    -- 3. Retrieval — top-8 relevant library chunks
    l_context := retrieve_context(p_message);

    -- 4. History — last 10 turns
    l_history := load_history(l_conv_id);

    -- 5. Build prompts
    l_system_prompt :=
      'You are FRP Studio''s assistant for iteria.us proposal writers. ' ||
      'Help with the current ERP proposal. ' ||
      'When you reference content from past proposals, cite the doc_id in brackets like [doc_id=abc123...]. ' ||
      'Quote exact language from the library when answering "what did we say about X" questions. ' ||
      'Be concise. ' ||
      'If a fact is not in the form state or library excerpts, say you do not know rather than guessing.' || CHR(10) || CHR(10) ||
      'WRITE TOOLS: when you want to change the form or attachments, ' ||
      'emit a tool_call block on its own line. Don''t ask permission first; ' ||
      'the user will see Apply / Reject buttons before any change happens.' || CHR(10) ||
      '<tool_call>{"tool":"set_form_field","args":{"field":"client_name","value":"City of Madison"}}</tool_call>' || CHR(10) ||
      '<tool_call>{"tool":"set_form_fields","args":{"updates":[{"field":"client_name","value":"X"},{"field":"industry","value":"Y"}]}}</tool_call>' || CHR(10) ||
      '<tool_call>{"tool":"attach_doc","args":{"doc_id":"<full doc_id from library citations>"}}</tool_call>' || CHR(10) ||
      '<tool_call>{"tool":"detach_doc","args":{"doc_id":"<doc_id>"}}</tool_call>' || CHR(10) ||
      'Valid form fields: client_name, industry, primary_contact, annual_budget, current_erp, rfp_number, due_date.';

    l_user_prompt := '';
    IF l_gist IS NOT NULL AND DBMS_LOB.GETLENGTH(l_gist) > 0 THEN
      l_user_prompt := 'Earlier in this conversation:' || CHR(10) ||
                       DBMS_LOB.SUBSTR(l_gist, 3000, 1) || CHR(10) || CHR(10);
    END IF;
    IF p_form_state IS NOT NULL AND DBMS_LOB.GETLENGTH(p_form_state) > 0 THEN
      l_user_prompt := l_user_prompt ||
        'Current proposal form state (JSON):' || CHR(10) ||
        DBMS_LOB.SUBSTR(p_form_state, 3000, 1) || CHR(10) || CHR(10);
    END IF;
    l_user_prompt := l_user_prompt ||
      'Library excerpts most relevant to my question:' || CHR(10) ||
      DBMS_LOB.SUBSTR(l_context, 16000, 1) || CHR(10) || CHR(10) ||
      'My question: ' || DBMS_LOB.SUBSTR(p_message, 4000, 1);

    -- 6. Build OCI chat messages array: system + history + new prompt
    j_msg := JSON_OBJECT_T();
    j_part := JSON_OBJECT_T();
    j_content := JSON_ARRAY_T();
    j_part.put('type', 'TEXT');
    j_part.put('text', l_system_prompt);
    j_content.append(j_part);
    j_msg.put('role', 'SYSTEM');
    j_msg.put('content', j_content);
    j_messages.append(j_msg);

    FOR i IN 0 .. l_history.get_size - 1 LOOP
      DECLARE
        j_h    JSON_OBJECT_T := TREAT(l_history.get(i) AS JSON_OBJECT_T);
        l_role VARCHAR2(20)  := UPPER(j_h.get_string('role'));
      BEGIN
        j_msg := JSON_OBJECT_T();
        j_part := JSON_OBJECT_T();
        j_content := JSON_ARRAY_T();
        j_part.put('type', 'TEXT');
        j_part.put('text', j_h.get_string('content'));
        j_content.append(j_part);
        j_msg.put('role', CASE WHEN l_role = 'ASSISTANT' THEN 'ASSISTANT' ELSE 'USER' END);
        j_msg.put('content', j_content);
        j_messages.append(j_msg);
      END;
    END LOOP;

    j_msg := JSON_OBJECT_T();
    j_part := JSON_OBJECT_T();
    j_content := JSON_ARRAY_T();
    j_part.put('type', 'TEXT');
    j_part.put('text', l_user_prompt);
    j_content.append(j_part);
    j_msg.put('role', 'USER');
    j_msg.put('content', j_content);
    j_messages.append(j_msg);

    j_chat.put('apiFormat',   'GENERIC');
    j_chat.put('messages',    j_messages);
    j_chat.put('maxTokens',   1500);
    j_chat.put('temperature', 0.3);
    j_chat.put('topP',        0.9);
    j_chat.put('isStream',    FALSE);

    j_serving.put('servingType', 'ON_DEMAND');
    j_serving.put('modelId',     C_GROK_OCID);

    j_root.put('compartmentId', C_OCI_COMPARTMENT_ID);
    j_root.put('servingMode',   j_serving);
    j_root.put('chatRequest',   j_chat);

    l_body := j_root.to_string;

    -- 7. Call OCI Grok
    l_resp := DBMS_CLOUD.SEND_REQUEST(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => C_GENAI_CHAT_URL,
      method          => 'POST',
      headers         => '{"Content-Type":"application/json"}',
      body            => UTL_RAW.CAST_TO_RAW(l_body)
    );

    l_status   := DBMS_CLOUD.GET_RESPONSE_STATUS_CODE(l_resp);
    l_resp_txt := DBMS_CLOUD.GET_RESPONSE_TEXT(l_resp);

    IF l_status != 200 THEN
      save_message(l_conv_id, 'tool_result',
        'API ERROR (HTTP ' || l_status || '): ' || SUBSTR(l_resp_txt, 1, 2000));
      RAISE_APPLICATION_ERROR(-20102,
        'Grok call failed (HTTP ' || l_status || '): ' || SUBSTR(l_resp_txt, 1, 2000));
    END IF;

    -- 8. Parse response
    l_answer  := JSON_VALUE(l_resp_txt,
                   '$.chatResponse.choices[0].message.content[0].text' RETURNING CLOB);
    l_in_tok  := JSON_VALUE(l_resp_txt, '$.chatResponse.usage.promptTokens'     RETURNING NUMBER);
    l_out_tok := JSON_VALUE(l_resp_txt, '$.chatResponse.usage.completionTokens' RETURNING NUMBER);
    l_elapsed := EXTRACT(SECOND FROM (SYSTIMESTAMP - l_started)) * 1000;

    -- Extract tool_calls from the answer (modifies l_answer in place)
    DECLARE
      l_tool_calls JSON_ARRAY_T;
    BEGIN
      parse_tool_calls(l_answer, l_tool_calls);

      -- Save assistant turn (with tool_calls if any)
      save_message(
        p_conv_id         => l_conv_id,
        p_role            => 'assistant',
        p_content         => l_answer,
        p_tool_calls_json => CASE WHEN l_tool_calls.get_size > 0
                                  THEN l_tool_calls.to_clob ELSE NULL END,
        p_input_tokens    => l_in_tok,
        p_output_tokens   => l_out_tok,
        p_provider        => 'oci',
        p_model           => 'grok'
      );

      -- Maybe roll up the gist
      maybe_update_gist(l_conv_id);

      -- 9. Return result envelope
      j_response := JSON_OBJECT_T();
      j_response.put('ok',              TRUE);
      j_response.put('conversation_id', l_conv_id);
      j_response.put('answer',          l_answer);
      j_response.put('tool_calls',      l_tool_calls);
      j_response.put('input_tokens',    l_in_tok);
      j_response.put('output_tokens',   l_out_tok);
      j_response.put('provider',        'oci');
      j_response.put('model',           'grok');
      j_response.put('elapsed_ms',      ROUND(l_elapsed));
      RETURN j_response.to_clob;
    END;
  END ask;

END FRP_COPILOT;
/

SHOW ERRORS PACKAGE BODY FRP_COPILOT;
