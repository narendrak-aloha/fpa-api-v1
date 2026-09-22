-- Run as the migration owner after migrations, never as an application login.
-- Supply login passwords outside source control, then GRANT fpa_runtime TO ... .
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fpa_runtime') THEN
    CREATE ROLE fpa_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA fpa_governance TO fpa_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA fpa_governance TO fpa_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA fpa_governance TO fpa_runtime;
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA fpa_governance TO fpa_runtime;
-- Accounts, roles and company scope are written by the app (signup, the
-- superadmin's access desk); the 017 triggers decide who may, not this grant.
REVOKE INSERT, UPDATE, DELETE ON fpa_governance.plan_state_transition FROM fpa_runtime;
REVOKE DELETE ON fpa_governance.app_user FROM fpa_runtime;
REVOKE UPDATE, DELETE, TRUNCATE ON fpa_governance.audit_event,
  fpa_governance.llm_disclosure_log FROM fpa_runtime;
REVOKE DELETE, TRUNCATE ON fpa_governance.agent_proposal FROM fpa_runtime;
-- No schema CREATE, table ownership, trigger disable, role grants or TRUNCATE.
-- fpa.actor is a trusted-server assertion. Never give this shared login to users.
