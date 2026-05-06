-- ═══════════════════════════════════════════════════════════════════
-- FRP_USER — package body, fixed
-- The in-DB body was INVALID due to PLS-00684 in get_profile_json:
-- JSON_OBJECT(... RETURNING CLOB) isn't accepted in PL/SQL function
-- context in this Oracle version (same bug we hit in FRP_INGEST.embed_one).
-- Only get_profile_json changed; all other procedures/functions are
-- byte-identical to the original.
--
-- Fix: build the outer JSON with JSON_OBJECT_T builder; parse the
-- already-built recent_usage CLOB as JSON_ARRAY_T so it nests cleanly
-- (preserving the FORMAT JSON semantics from the original).
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE OR REPLACE PACKAGE BODY FRP_USER AS

  --------------------------------------------------------------------------
  PROCEDURE upsert_profile_row(p_user_id IN VARCHAR2) IS
    l_count NUMBER;
  BEGIN
    SELECT COUNT(*) INTO l_count FROM iteria_ai.user_profiles WHERE user_id = p_user_id;
    IF l_count = 0 THEN
      INSERT INTO iteria_ai.user_profiles(user_id) VALUES (p_user_id);
    END IF;
  END;

  --------------------------------------------------------------------------
  PROCEDURE save_anthropic_key(p_user_id IN VARCHAR2,
                               p_key     IN VARCHAR2) IS
    l_cipher BLOB;
    l_iv     RAW(16);
    l_hint   VARCHAR2(8);
  BEGIN
    upsert_profile_row(p_user_id);

    iteria_ai.frp_vault.encrypt_key(p_key, l_cipher, l_iv);
    l_hint := SUBSTR(p_key, -4);

    UPDATE iteria_ai.user_profiles
       SET anthropic_key_enc  = l_cipher,
           anthropic_key_iv   = l_iv,
           anthropic_key_hint = l_hint,
           key_added_at       = CURRENT_TIMESTAMP,
           updated_at         = CURRENT_TIMESTAMP
     WHERE user_id = p_user_id;
    COMMIT;
  END save_anthropic_key;

  --------------------------------------------------------------------------
  PROCEDURE remove_anthropic_key(p_user_id IN VARCHAR2) IS
  BEGIN
    UPDATE iteria_ai.user_profiles
       SET anthropic_key_enc  = NULL,
           anthropic_key_iv   = NULL,
           anthropic_key_hint = NULL,
           key_added_at       = NULL,
           key_last_used_at   = NULL,
           updated_at         = CURRENT_TIMESTAMP
     WHERE user_id = p_user_id;
    COMMIT;
  END remove_anthropic_key;

  --------------------------------------------------------------------------
  FUNCTION get_anthropic_key(p_user_id IN VARCHAR2) RETURN VARCHAR2 IS
    l_cipher BLOB;
    l_iv     RAW(16);
  BEGIN
    BEGIN
      SELECT anthropic_key_enc, anthropic_key_iv
        INTO l_cipher, l_iv
        FROM iteria_ai.user_profiles
       WHERE user_id = p_user_id;
    EXCEPTION WHEN NO_DATA_FOUND THEN RETURN NULL; END;

    RETURN iteria_ai.frp_vault.decrypt_key(l_cipher, l_iv);
  END get_anthropic_key;

  --------------------------------------------------------------------------
  FUNCTION has_anthropic_key(p_user_id IN VARCHAR2) RETURN BOOLEAN IS
    l_cnt NUMBER;
  BEGIN
    SELECT COUNT(*)
      INTO l_cnt
      FROM iteria_ai.user_profiles
     WHERE user_id = p_user_id
       AND anthropic_key_enc IS NOT NULL;
    RETURN l_cnt > 0;
  END has_anthropic_key;

  --------------------------------------------------------------------------
  PROCEDURE record_usage(p_user_id      IN VARCHAR2,
                         p_provider     IN VARCHAR2,
                         p_model        IN VARCHAR2,
                         p_action_desc  IN VARCHAR2,
                         p_context_ref  IN VARCHAR2,
                         p_input_tokens IN NUMBER,
                         p_output_tokens IN NUMBER,
                         p_cost_usd     IN NUMBER,
                         p_proposal_id  IN VARCHAR2 DEFAULT NULL) IS
  BEGIN
    INSERT INTO iteria_ai.user_api_usage
      (user_id, provider, model, action_desc, context_ref,
       input_tokens, output_tokens, cost_usd, proposal_id)
    VALUES
      (p_user_id, p_provider, p_model, p_action_desc, p_context_ref,
       p_input_tokens, p_output_tokens, p_cost_usd, p_proposal_id);
  END record_usage;

  --------------------------------------------------------------------------
  PROCEDURE touch_last_used(p_user_id IN VARCHAR2) IS
  BEGIN
    UPDATE iteria_ai.user_profiles
       SET key_last_used_at = CURRENT_TIMESTAMP
     WHERE user_id = p_user_id;
  END touch_last_used;

  --------------------------------------------------------------------------
  -- FIXED: outer JSON built with JSON_OBJECT_T builder. recent_usage
  -- is parsed as a JSON_ARRAY_T so it nests as JSON, not as a string.
  FUNCTION get_profile_json(p_user_id IN VARCHAR2) RETURN CLOB IS
    l_has_key       VARCHAR2(5) := 'false';
    l_hint          VARCHAR2(8);
    l_added         TIMESTAMP(6);
    l_last_used     TIMESTAMP(6);
    l_usage_array   CLOB;
    l_first         BOOLEAN := TRUE;
    l_out           CLOB;
    j_root          JSON_OBJECT_T := JSON_OBJECT_T();
    j_usage         JSON_ARRAY_T;
  BEGIN
    BEGIN
      SELECT CASE WHEN anthropic_key_enc IS NULL THEN 'false' ELSE 'true' END,
             anthropic_key_hint,
             key_added_at,
             key_last_used_at
        INTO l_has_key, l_hint, l_added, l_last_used
        FROM iteria_ai.user_profiles
       WHERE user_id = p_user_id;
    EXCEPTION WHEN NO_DATA_FOUND THEN
      l_has_key := 'false';
    END;

    DBMS_LOB.CREATETEMPORARY(l_usage_array, TRUE);
    DBMS_LOB.WRITEAPPEND(l_usage_array, 1, '[');
    FOR r IN (
      SELECT TO_CHAR(used_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS used_at_iso,
             provider, model, action_desc, context_ref,
             input_tokens, output_tokens, cost_usd, proposal_id
        FROM iteria_ai.user_api_usage
       WHERE user_id = p_user_id
       ORDER BY used_at DESC
    ) LOOP
      IF NOT l_first THEN DBMS_LOB.WRITEAPPEND(l_usage_array, 1, ','); END IF;
      l_first := FALSE;
      DBMS_LOB.APPEND(l_usage_array, JSON_OBJECT(
        'used_at'       VALUE r.used_at_iso,
        'provider'      VALUE r.provider,
        'model'         VALUE r.model,
        'action_desc'   VALUE r.action_desc,
        'context_ref'   VALUE r.context_ref,
        'input_tokens'  VALUE r.input_tokens,
        'output_tokens' VALUE r.output_tokens,
        'cost_usd'      VALUE r.cost_usd,
        'proposal_id'   VALUE r.proposal_id
      ));
    END LOOP;
    DBMS_LOB.WRITEAPPEND(l_usage_array, 1, ']');

    j_usage := JSON_ARRAY_T.parse(l_usage_array);
    j_root.put('user_id',      p_user_id);
    j_root.put('has_key',      CASE WHEN l_has_key = 'true' THEN TRUE ELSE FALSE END);
    j_root.put('key_hint',     l_hint);
    j_root.put('added_at',     TO_CHAR(l_added,     'YYYY-MM-DD"T"HH24:MI:SS'));
    j_root.put('last_used_at', TO_CHAR(l_last_used, 'YYYY-MM-DD"T"HH24:MI:SS'));
    j_root.put('recent_usage', j_usage);

    l_out := j_root.to_clob;

    DBMS_LOB.FREETEMPORARY(l_usage_array);
    RETURN l_out;
  END get_profile_json;

END FRP_USER;
/

SHOW ERRORS PACKAGE BODY FRP_USER;
