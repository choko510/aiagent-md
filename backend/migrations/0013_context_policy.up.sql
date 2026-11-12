-- 0013_context_policy: user-granted Context/Memory egress policy.
--
-- Switching the Context path is NOT an environment variable: the authenticated
-- owner enables it per task (recipient + model) and the Model Gateway re-reads
-- the policy through a claim-bound function before every admission. Memory
-- consent for a recipient is a separate durable grant so withdrawal is explicit.

CREATE TABLE agent.memory_consents (
  tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  granted_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version >= 1),
  PRIMARY KEY (tenant_id, owner_user_id, recipient)
);
CREATE TABLE agent.task_context_policies (
  tenant_id uuid NOT NULL, task_id uuid NOT NULL, owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
  enabled boolean NOT NULL DEFAULT true,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, task_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);
-- API rows are tenant-scoped exactly like the existing tables (the authenticated
-- token resolves to one tenant; owner predicates are additionally in SQL).
ALTER TABLE agent.memory_consents ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_tenant ON agent.memory_consents TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY service_all ON agent.memory_consents TO aiagent_ops USING (true) WITH CHECK (true);
ALTER TABLE agent.task_context_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_tenant ON agent.task_context_policies TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY service_all ON agent.task_context_policies TO aiagent_ops USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON agent.memory_consents, agent.task_context_policies TO aiagent_api;
GRANT SELECT ON agent.memory_consents, agent.task_context_policies TO aiagent_ops;

-- Claim-bound reads for the Model Gateway (no direct table privilege).
CREATE FUNCTION agent.context_policy(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

CREATE FUNCTION agent.context_dispatch_unresolved(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (SELECT 1 FROM agent.context_dispatches d
    WHERE d.tenant_id = p_tenant AND d.run_id = p_run AND d.status_code IN ('ADMITTED','UNKNOWN'));
END $$;
REVOKE ALL ON FUNCTION agent.context_dispatch_unresolved(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_dispatch_unresolved(uuid, uuid, uuid, uuid, bigint, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_consent_ok(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_recipient text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (SELECT 1 FROM agent.memory_consents m
    WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.recipient = p_recipient
      AND m.revoked_at IS NULL);
END $$;
REVOKE ALL ON FUNCTION agent.memory_consent_ok(uuid, uuid, uuid, uuid, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_consent_ok(uuid, uuid, uuid, uuid, bigint, text, text)
  TO aiagent_model_gateway;
