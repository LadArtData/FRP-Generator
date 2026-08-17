-- ============================================================
-- WARDEN — Worker package
-- Job claim / complete / fail (SCOUT pattern)
-- ============================================================

SET DEFINE OFF
ALTER SESSION SET CURRENT_SCHEMA = iteria_ai;

CREATE OR REPLACE PACKAGE pkg_warden_worker AS
  PROCEDURE claim_pending_job(p_api_key IN VARCHAR2, p_status OUT PLS_INTEGER, p_json OUT CLOB);
  PROCEDURE complete_job(p_job_id IN NUMBER, p_status OUT PLS_INTEGER, p_json OUT CLOB);
  PROCEDURE fail_job(p_job_id IN NUMBER, p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB);
  PROCEDURE enqueue_job(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB);
END pkg_warden_worker;
/

CREATE OR REPLACE PACKAGE BODY pkg_warden_worker AS

  FUNCTION authorized(p_api_key IN VARCHAR2) RETURN BOOLEAN IS
    l_key VARCHAR2(4000);
  BEGIN
    BEGIN
      SELECT api_key INTO l_key FROM api_configuration WHERE is_active = 'Y' AND ROWNUM = 1;
    EXCEPTION
      WHEN NO_DATA_FOUND THEN RETURN TRUE;
    END;
    IF l_key IS NULL THEN RETURN TRUE; END IF;
    RETURN p_api_key = l_key;
  END authorized;

  PROCEDURE claim_pending_job(p_api_key IN VARCHAR2, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
    l_job_id NUMBER;
    l_claimed NUMBER := 0;
    l_type  VARCHAR2(30);
    l_tid   NUMBER;
    l_payload CLOB;
  BEGIN
    IF NOT authorized(p_api_key) THEN
      p_status := 403;
      p_json := '{"items":[],"error":"unauthorized"}';
      RETURN;
    END IF;

    BEGIN
      UPDATE warden_jobs
         SET status = 'running',
             claimed_at = SYSTIMESTAMP,
             started_at = NVL(started_at, SYSTIMESTAMP)
       WHERE job_id = (
         SELECT job_id FROM (
           SELECT job_id FROM warden_jobs WHERE status = 'pending' ORDER BY created_at
         ) WHERE ROWNUM = 1
       )
      RETURNING job_id INTO l_job_id;
      l_claimed := SQL%ROWCOUNT;
      COMMIT;
    EXCEPTION
      WHEN NO_DATA_FOUND THEN l_claimed := 0;
    END;

    IF l_claimed = 0 THEN
      p_status := 200;
      p_json := '{"items":[]}';
      RETURN;
    END IF;

    SELECT job_type, tenant_id, payload_json
      INTO l_type, l_tid, l_payload
      FROM warden_jobs WHERE job_id = l_job_id;

    p_status := 200;
    p_json := '{"items":[{"job_id":' || l_job_id
      || ',"job_type":"' || REPLACE(l_type, '"', '\"') || '"'
      || ',"tenant_id":' || NVL(TO_CHAR(l_tid), 'null')
      || ',"payload_json":' || NVL(l_payload, TO_CLOB('{}')) || '}]}';
  END claim_pending_job;

  PROCEDURE complete_job(p_job_id IN NUMBER, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
  BEGIN
    UPDATE warden_jobs
       SET status = 'done', completed_at = SYSTIMESTAMP
     WHERE job_id = p_job_id;
    COMMIT;
    p_status := 200;
    p_json := '{"ok":true}';
  END complete_job;

  PROCEDURE fail_job(p_job_id IN NUMBER, p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
  BEGIN
    UPDATE warden_jobs
       SET status = 'failed',
           error_message = p_body,
           completed_at = SYSTIMESTAMP
     WHERE job_id = p_job_id;
    COMMIT;
    p_status := 200;
    p_json := '{"ok":true}';
  END fail_job;

  PROCEDURE enqueue_job(p_body IN CLOB, p_status OUT PLS_INTEGER, p_json OUT CLOB) IS
    l_type VARCHAR2(30);
    l_tid  NUMBER;
    l_payload CLOB;
    l_id   NUMBER;
  BEGIN
    APEX_JSON.PARSE(p_body);
    l_type := APEX_JSON.GET_VARCHAR2('job_type');
    l_tid := APEX_JSON.GET_NUMBER('tenant_id');
    l_payload := APEX_JSON.GET_CLOB('payload');

    INSERT INTO warden_jobs (job_type, tenant_id, payload_json, status)
    VALUES (l_type, l_tid, NVL(l_payload, '{}'), 'pending')
    RETURNING job_id INTO l_id;

    COMMIT;
    p_status := 200;
    p_json := '{"ok":true,"job_id":' || l_id || '}';
  END enqueue_job;

END pkg_warden_worker;
/

SHOW ERRORS PACKAGE pkg_warden_worker;
SHOW ERRORS PACKAGE BODY pkg_warden_worker;

GRANT EXECUTE ON pkg_warden_worker TO admin;
