-- Example: create a tenant row (no client data)
-- Run as ITERIA_AI after schema.sql

ALTER SESSION SET CURRENT_SCHEMA = iteria_ai;

INSERT INTO warden_tenants (name, env, bucket_prefix, status)
VALUES ('Your Organization', 'Production', 'tenants/your-org/', 'active');

COMMIT;

-- Note the generated tenant_id:
SELECT tenant_id, name, env FROM warden_tenants ORDER BY tenant_id DESC FETCH FIRST 1 ROW ONLY;
