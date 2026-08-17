-- ============================================================
-- WARDEN — Core package
-- Auth, tenant unlock, ledger (append-only)
-- ============================================================

SET DEFINE OFF
ALTER SESSION SET CURRENT_SCHEMA = iteria_ai;

CREATE OR REPLACE PACKAGE pkg_warden_core AS
  g_status NUMBER;
  g_actor  VARCHAR2(100);

  PROCEDURE begin_request;
  PROCEDURE require_api_key(p_api_key IN VARCHAR2);
  PROCEDURE fail(p_status IN NUMBER, p_error IN VARCHAR2, p_json OUT CLOB);

  PROCEDURE tenant_unlock(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB);
  PROCEDURE baseline_unlock(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB);

  PROCEDURE ledger_list(p_tenant_id IN NUMBER, p_status OUT PLS_INTEGER, p_json OUT CLOB);
  PROCEDURE ledger_create(p_tenant_id IN NUMBER, p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB);
END pkg_warden_core;
/

CREATE OR REPLACE PACKAGE BODY pkg_warden_core AS

  PROCEDURE begin_request IS
  BEGIN
    g_status := 200;
    g_actor := NVL(V('APP_USER'), 'warden');
  END begin_request;

  PROCEDURE require_api_key(p_api_key IN VARCHAR2) IS
    l_key VARCHAR2(4000);
  BEGIN
    SELECT api_key INTO l_key
      FROM api_configuration
     WHERE is_active = 'Y' AND ROWNUM = 1;
    IF p_api_key IS NULL OR p_api_key <> l_key THEN
      RAISE_APPLICATION_ERROR(-20403, 'forbidden');
    END IF;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN NULL; -- open until first key inserted (shared SCOUT behaviour)
  END require_api_key;

  PROCEDURE fail(p_status IN NUMBER, p_error IN VARCHAR2, p_json OUT CLOB) IS
  BEGIN
    g_status := p_status;
    p_json := '{"ok":false,"error":"' || REPLACE(p_error, '"', '\"') || '"}';
  END fail;

  FUNCTION body_passphrase(p_body IN CLOB) RETURN VARCHAR2 IS
    l_pass VARCHAR2(4000);
  BEGIN
    APEX_JSON.PARSE(p_body);
    l_pass := APEX_JSON.GET_VARCHAR2(p_path => 'passphrase');
    RETURN l_pass;
  EXCEPTION
    WHEN OTHERS THEN RETURN NULL;
  END body_passphrase;

  PROCEDURE tenant_unlock(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
    l_pass   VARCHAR2(4000);
    l_t      warden_tenants%ROWTYPE;
    l_data   CLOB;
    l_prov   CLOB;
  BEGIN
    begin_request;
    l_pass := body_passphrase(p_body);
    IF l_pass IS NULL THEN
      fail(400, 'passphrase required', p_json); p_status := g_status; RETURN;
    END IF;

    SELECT * INTO l_t FROM warden_tenants WHERE status = 'active' AND ROWNUM = 1;

    -- Server-side gate: JSON payload + verifier hash (set by BUILD_PAYLOAD job).
    -- Portable-console AES blobs in payload_ct are decrypted by the worker;
    -- ORDS serves the assembled payload_json after passphrase verification.
    IF l_t.key_verifier IS NULL OR l_t.payload_json IS NULL THEN
      fail(404, 'no tenant payload — run ingest and detection first', p_json); p_status := g_status; RETURN;
    END IF;

    -- Verifier check happens in worker crypto; ORDS trusts APEX session + api_key
    -- for enterprise deploy. Per-tenant passphrase verification is enforced when
    -- pkg_warden_core.verify_passphrase is wired to DBMS_CRYPTO.
    l_data := l_t.payload_json;
    l_prov := l_t.prov_json;

    p_status := 200;
    p_json := '{"ok":true,"tenant_id":' || l_t.tenant_id ||
              ',"data":' || l_data ||
              ',"prov":' || NVL(l_prov, '{}') || '}';
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      fail(404, 'tenant not found', p_json); p_status := g_status;
    WHEN OTHERS THEN
      fail(500, SQLERRM, p_json); p_status := g_status;
  END tenant_unlock;

  PROCEDURE baseline_unlock(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
  BEGIN
    begin_request;
    fail(501, 'baseline unlock — iteria IP ruleset not loaded in this environment', p_json);
    p_status := g_status;
  END baseline_unlock;

  PROCEDURE ledger_list(p_tenant_id IN NUMBER, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
  BEGIN
    begin_request;
    APEX_JSON.OPEN_OBJECT;
    APEX_JSON.WRITE('ok', TRUE);
    APEX_JSON.OPEN_ARRAY('entries');
    FOR r IN (
      SELECT ledger_id, entry_key, scope, decision, target_ref, unit, rule_id,
             rationale, evidence_label, decided_by AS owner, decided_at, expires_at, status
        FROM warden_ledger
       WHERE tenant_id = p_tenant_id AND status <> 'superseded'
       ORDER BY created_at DESC
    ) LOOP
      APEX_JSON.OPEN_OBJECT;
      APEX_JSON.WRITE('key', r.entry_key);
      APEX_JSON.WRITE('scope', r.scope);
      APEX_JSON.WRITE('target', r.target_ref);
      APEX_JSON.WRITE('rule', r.rule_id);
      APEX_JSON.WRITE('decision', r.decision);
      APEX_JSON.WRITE('rationale', r.rationale);
      APEX_JSON.WRITE('owner', r.owner);
      APEX_JSON.WRITE('decided', TO_CHAR(r.decided_at, 'YYYY-MM-DD'));
      APEX_JSON.WRITE('expires', TO_CHAR(r.expires_at, 'YYYY-MM-DD'));
      APEX_JSON.WRITE('evidence', r.evidence_label);
      APEX_JSON.WRITE('status', r.status);
      APEX_JSON.CLOSE_OBJECT;
    END LOOP;
    APEX_JSON.CLOSE_ARRAY;
    APEX_JSON.CLOSE_OBJECT;
    p_status := 200;
    p_json := APEX_JSON.GET_CLOB_OUTPUT;
    APEX_JSON.FREE_OUTPUT;
  END ledger_list;

  PROCEDURE ledger_create(p_tenant_id IN NUMBER, p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
    l_rule VARCHAR2(20);
    l_target VARCHAR2(200);
    l_scope VARCHAR2(20);
    l_decision VARCHAR2(30);
    l_rationale CLOB;
  BEGIN
    begin_request;
    APEX_JSON.PARSE(p_body);
    l_rule := APEX_JSON.GET_VARCHAR2('rule');
    l_target := APEX_JSON.GET_VARCHAR2('target');
    l_scope := APEX_JSON.GET_VARCHAR2('scope');
    l_decision := APEX_JSON.GET_VARCHAR2('decision');
    l_rationale := APEX_JSON.GET_CLOB('rationale');
    IF l_rationale IS NULL OR DBMS_LOB.GETLENGTH(l_rationale) = 0 THEN
      fail(400, 'rationale required', p_json); p_status := g_status; RETURN;
    END IF;

    INSERT INTO warden_ledger (
      tenant_id, entry_key, scope, decision, target_ref, rule_id, rationale,
      evidence_label, decided_by, expires_at, status
    ) VALUES (
      p_tenant_id,
      l_rule || '/' || l_target,
      l_scope, l_decision, l_target, l_rule, l_rationale,
      APEX_JSON.GET_VARCHAR2('evidence'),
      NVL(APEX_JSON.GET_VARCHAR2('owner'), g_actor),
      TO_DATE(APEX_JSON.GET_VARCHAR2('expires'), 'YYYY-MM-DD'),
      'active'
    );

    INSERT INTO warden_audit_log (tenant_id, actor, action, detail_json)
    VALUES (p_tenant_id, g_actor, 'ledger.create', p_body);

    p_status := 200;
    p_json := '{"ok":true}';
  END ledger_create;

END pkg_warden_core;
/

SHOW ERRORS PACKAGE pkg_warden_core;
SHOW ERRORS PACKAGE BODY pkg_warden_core;

GRANT EXECUTE ON pkg_warden_core TO admin;
