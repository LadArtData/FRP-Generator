-- ═══════════════════════════════════════════════════════════════════
-- C6 — Anthropic / Claude override setup
-- Run as ADMIN (not ITERIA_AI). Required ONCE per ADB instance before
-- the FRP_COPILOT package can reach Claude. After this:
--   - ITERIA_AI can call api.anthropic.com over HTTPS
--   - FRP_COPILOT.ask will use Claude when the user has saved a key
--   - It silently falls back to Grok if Anthropic returns any error
-- ═══════════════════════════════════════════════════════════════════

-- Grant the ITERIA_AI schema network access to Anthropic's API host.
-- This is what enables DBMS_CLOUD.SEND_REQUEST to hit api.anthropic.com.
BEGIN
  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
    host => 'api.anthropic.com',
    ace  => xs$ace_type(
      privilege_list => xs$name_list('http', 'http_proxy'),
      principal_name => 'ITERIA_AI',
      principal_type => xs_acl.ptype_db
    )
  );
END;
/

-- Verify it took
SELECT host, principal, lower_port, upper_port
  FROM dba_host_acls
 WHERE host = 'api.anthropic.com';

-- Verify ITERIA_AI specifically can resolve + reach the host
SELECT UTL_HTTP.REQUEST(
  'https://api.anthropic.com/v1/messages',
  NULL, NULL,
  UTL_HTTP.HTTPS_HOST_CHECK_NONE
) FROM dual;
-- Expected: a 401 or 405 response body (the API rejecting an unauthenticated
-- request is FINE — it means the network path works). If you get
-- ORA-24247 "network access denied by access control list (ACL)",
-- the grant above didn't apply; check principal_name spelling.

-- ─────────────────────────────────────────────────────────
-- Per-user setup (run as ITERIA_AI, once per user who wants Claude):
--
--   BEGIN
--     iteria_ai.frp_user.save_anthropic_key(
--       p_user_id => 'brian.schell@iteria.us',
--       p_key     => 'sk-ant-api03-XXXXXXXX...'
--     );
--   END;
--   /
--
-- Verify with:
--   SELECT iteria_ai.frp_user.has_anthropic_key('brian.schell@iteria.us')
--     FROM dual;  -- expect TRUE
--
-- The key is encrypted via FRP_VAULT before storage. Plaintext is
-- never persisted. To remove a key:
--   BEGIN iteria_ai.frp_user.remove_anthropic_key('brian.schell@iteria.us'); END;
--   /
-- ─────────────────────────────────────────────────────────
