-- 0006_step4_context_memory: squashed hardened schema for Context artifacts,
-- claim-bound Model Gateway access, Memory scope/records/consent history, and
-- external tombstone runtime/restore coverage (replacing intermediate 0006..0016).
--
-- The Model Gateway role NEVER holds direct DML/SELECT privileges or permissive
-- RLS policies on Context/Memory tables. Every Model Gateway operation passes
-- through a SECURITY DEFINER routine that validates the active Run lease claim.

-- ---------------------------------------------------------------- Shared authority helpers
CREATE FUNCTION agent.scope_active(p_tenant uuid, p_owner uuid, p_workspace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (
    SELECT 1 FROM agent.tenants t
      JOIN agent.tenant_memberships m ON m.tenant_id = t.id AND m.user_id = p_owner
      JOIN agent.users u ON u.id = m.user_id
      JOIN agent.workspaces w ON w.tenant_id = t.id AND w.owner_user_id = m.user_id AND w.id = p_workspace
     WHERE t.id = p_tenant AND t.disabled_at IS NULL AND u.disabled_at IS NULL
       AND m.disabled_at IS NULL AND w.disabled_at IS NULL)
$$;
REVOKE ALL ON FUNCTION agent.scope_active(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.scope_active(uuid, uuid, uuid) TO
  aiagent_api, aiagent_runtime, aiagent_model_gateway, aiagent_tool_gateway, aiagent_sentinel, aiagent_ops;

CREATE FUNCTION agent.context_claim_valid(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (
    SELECT 1 FROM agent.runs r
      JOIN agent.tasks t ON t.tenant_id = r.tenant_id AND t.id = r.task_id
      CROSS JOIN agent.schema_control s
     WHERE r.tenant_id = p_tenant AND r.id = p_run AND r.task_id = p_task
       AND t.owner_user_id = p_owner AND t.active_run_id = p_run
       AND r.lease_epoch = p_lease AND r.lease_owner_ref = p_worker
       AND r.lease_expires_at > clock_timestamp()
       AND r.status_code NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED',
                                 'BUDGET_EXHAUSTED','CANCELLING','PAUSED')
       AND NOT r.cancel_requested AND NOT r.pause_requested
       AND r.restore_incarnation = s.restore_incarnation
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id))
$$;
REVOKE ALL ON FUNCTION agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text) TO
  aiagent_model_gateway, aiagent_ops;

-- ---------------------------------------------------------------- Context artifacts
CREATE TABLE agent.context_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('raw', 'summary')),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 2000000),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext bytea,
  key_id text NOT NULL DEFAULT 'legacy' CHECK (key_id ~ '^[A-Za-z0-9._-]{1,64}$'),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  CHECK (expires_at > created_at),
  CHECK ((revoked_at IS NULL) = (ciphertext IS NOT NULL))
);
CREATE INDEX context_artifacts_expiry ON agent.context_artifacts (expires_at) WHERE revoked_at IS NULL;
ALTER TABLE agent.context_artifacts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.context_artifacts FROM PUBLIC;
GRANT SELECT ON agent.context_artifacts TO aiagent_ops;

-- ---------------------------------------------------------------- Memory scope, records & history
CREATE TABLE agent.memory_scope_epochs (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  task_id uuid NOT NULL,
  source_epoch bigint NOT NULL DEFAULT 1 CHECK (source_epoch BETWEEN 1 AND 9223372036854775807),
  PRIMARY KEY (tenant_id, owner_user_id, task_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);

CREATE TABLE agent.memory_records (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  task_id uuid NOT NULL,
  id uuid NOT NULL,
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  source_artifact_id uuid NOT NULL,
  projection_artifact_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  observed_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, owner_user_id, task_id) REFERENCES agent.memory_scope_epochs(tenant_id, owner_user_id, task_id),
  FOREIGN KEY (tenant_id, source_artifact_id) REFERENCES agent.context_artifacts(tenant_id, id),
  FOREIGN KEY (tenant_id, projection_artifact_id) REFERENCES agent.context_artifacts(tenant_id, id),
  CHECK (expires_at > created_at)
);
CREATE INDEX memory_records_scope ON agent.memory_records(tenant_id, owner_user_id, task_id) WHERE revoked_at IS NULL;
ALTER TABLE agent.memory_scope_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.memory_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_epoch_scope ON agent.memory_scope_epochs TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));
CREATE POLICY memory_record_scope ON agent.memory_records TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));
GRANT SELECT, INSERT, UPDATE ON agent.memory_scope_epochs, agent.memory_records TO aiagent_api;
GRANT SELECT ON agent.memory_scope_epochs, agent.memory_records TO aiagent_ops;

CREATE FUNCTION agent.guard_memory_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (TG_OP = 'INSERT' AND NEW.source_epoch <> 1) OR (TG_OP = 'UPDATE' AND
    (NEW.source_epoch <> OLD.source_epoch + 1 OR (NEW.tenant_id, NEW.owner_user_id, NEW.task_id)
     IS DISTINCT FROM (OLD.tenant_id, OLD.owner_user_id, OLD.task_id))) THEN
    RAISE EXCEPTION 'invalid memory epoch transition' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER memory_epoch_guard BEFORE INSERT OR UPDATE ON agent.memory_scope_epochs
  FOR EACH ROW EXECUTE FUNCTION agent.guard_memory_epoch();

CREATE FUNCTION agent.guard_memory_record() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  PERFORM 1 FROM agent.memory_scope_epochs e WHERE e.tenant_id = NEW.tenant_id
    AND e.owner_user_id = NEW.owner_user_id AND e.task_id = NEW.task_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'memory scope missing' USING ERRCODE = 'check_violation'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.revision <> 1 OR NEW.revoked_at IS NOT NULL OR NOT EXISTS
      (SELECT 1 FROM agent.tasks t WHERE t.tenant_id = NEW.tenant_id AND t.id = NEW.task_id
        AND t.owner_user_id = NEW.owner_user_id) THEN
      RAISE EXCEPTION 'invalid memory owner' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM agent.context_artifacts a WHERE a.tenant_id = NEW.tenant_id
      AND a.owner_user_id = NEW.owner_user_id AND a.task_id = NEW.task_id
      AND a.id IN (NEW.source_artifact_id, NEW.projection_artifact_id)
      AND a.kind = 'raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp() FOR SHARE;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> (CASE WHEN NEW.source_artifact_id = NEW.projection_artifact_id THEN 1 ELSE 2 END) THEN
      RAISE EXCEPTION 'invalid memory source' USING ERRCODE = 'check_violation';
    END IF;
  ELSIF OLD.revoked_at IS NOT NULL THEN
    RAISE EXCEPTION 'memory already revoked' USING ERRCODE = 'check_violation';
  ELSIF NEW.revision <> OLD.revision + 1
        AND NOT (current_setting('aiagent.memory_restore_reconcile', true) = 'on'
                 AND NEW.revision > OLD.revision) THEN
    RAISE EXCEPTION 'invalid memory revision' USING ERRCODE = 'check_violation';
  ELSIF (NEW.tenant_id, NEW.owner_user_id, NEW.task_id, NEW.id, NEW.source_artifact_id,
         NEW.projection_artifact_id, NEW.consent_basis, NEW.observed_at, NEW.created_at) IS DISTINCT FROM
        (OLD.tenant_id, OLD.owner_user_id, OLD.task_id, OLD.id, OLD.source_artifact_id,
         OLD.projection_artifact_id, OLD.consent_basis, OLD.observed_at, OLD.created_at) THEN
    RAISE EXCEPTION 'invalid memory mutation' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1 WHERE tenant_id = NEW.tenant_id
    AND owner_user_id = NEW.owner_user_id AND task_id = NEW.task_id;
  RETURN NEW;
END $$;
CREATE TRIGGER memory_record_guard BEFORE INSERT OR UPDATE ON agent.memory_records
  FOR EACH ROW EXECUTE FUNCTION agent.guard_memory_record();

CREATE TABLE agent.memory_record_revisions (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  task_id uuid NOT NULL,
  memory_id uuid NOT NULL,
  revision bigint NOT NULL CHECK (revision >= 1),
  change_kind text NOT NULL CHECK (change_kind IN ('created','updated','revoked')),
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  expires_at timestamptz NOT NULL,
  actor_user_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, memory_id, revision),
  FOREIGN KEY (tenant_id, memory_id) REFERENCES agent.memory_records(tenant_id, id)
);
CREATE INDEX memory_revision_task ON agent.memory_record_revisions(tenant_id, owner_user_id, task_id, recorded_at DESC);
ALTER TABLE agent.memory_record_revisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_revision_scope ON agent.memory_record_revisions TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));
GRANT SELECT, INSERT ON agent.memory_record_revisions TO aiagent_api;
GRANT SELECT ON agent.memory_record_revisions TO aiagent_ops;
CREATE TRIGGER memory_revision_append_only BEFORE UPDATE OR DELETE ON agent.memory_record_revisions
  FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();

-- ---------------------------------------------------------------- Restore state & tombstones
CREATE TABLE agent.memory_restore_state (
  restore_incarnation bigint PRIMARY KEY CHECK (restore_incarnation >= 1),
  external_generation bigint NOT NULL CHECK (external_generation >= 1),
  tombstone_chain_head char(64) NOT NULL CHECK (tombstone_chain_head ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz NOT NULL,
  reconciled_at timestamptz NOT NULL DEFAULT now(),
  reconciled_by text NOT NULL CHECK (length(reconciled_by) BETWEEN 1 AND 200)
);
REVOKE ALL ON agent.memory_restore_state FROM PUBLIC;
GRANT SELECT ON agent.memory_restore_state TO aiagent_api, aiagent_model_gateway, aiagent_ops;
GRANT INSERT, UPDATE ON agent.memory_restore_state TO aiagent_ops;

CREATE TABLE agent.memory_tombstone_watermarks (
  generation bigint PRIMARY KEY CHECK (generation >= 1),
  chain_head char(64) NOT NULL CHECK (chain_head ~ '^[0-9a-f]{64}$'),
  last_event_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON agent.memory_tombstone_watermarks TO aiagent_api, aiagent_ops;
GRANT INSERT ON agent.memory_tombstone_watermarks TO aiagent_ops;

CREATE TABLE agent.memory_tombstone_coverage (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  covered_revocation_generation bigint NOT NULL DEFAULT 0 CHECK (covered_revocation_generation >= 0),
  chain_head char(64) NOT NULL DEFAULT repeat('0', 64) CHECK (chain_head ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO agent.memory_tombstone_coverage(singleton) VALUES (true);
REVOKE ALL ON agent.memory_tombstone_coverage FROM PUBLIC;
GRANT SELECT, UPDATE ON agent.memory_tombstone_coverage TO aiagent_api, aiagent_ops;

-- ---------------------------------------------------------------- Context dispatches
CREATE TABLE agent.context_dispatches (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  head_revision bigint NOT NULL,
  model_attempt_id uuid NOT NULL,
  status_code text NOT NULL CHECK (status_code IN ('ADMITTED','COMPLETED','UNKNOWN')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id, head_revision),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, run_id, head_revision) REFERENCES agent.run_checkpoints(tenant_id, run_id, checkpoint_revision),
  FOREIGN KEY (tenant_id, model_attempt_id) REFERENCES agent.model_attempts(tenant_id, id)
);
CREATE UNIQUE INDEX context_dispatch_unresolved_run
  ON agent.context_dispatches(tenant_id, run_id)
  WHERE status_code IN ('ADMITTED','UNKNOWN');
ALTER TABLE agent.context_dispatches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.context_dispatches FROM PUBLIC;
GRANT SELECT ON agent.context_dispatches TO aiagent_ops;

CREATE FUNCTION agent.guard_context_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status_code <> 'ADMITTED' OR NEW.status_code NOT IN ('COMPLETED','UNKNOWN') THEN
    RAISE EXCEPTION 'invalid context dispatch transition' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER context_dispatch_guard BEFORE UPDATE ON agent.context_dispatches
  FOR EACH ROW EXECUTE FUNCTION agent.guard_context_dispatch();

-- ---------------------------------------------------------------- User-granted Context/Memory egress policy
CREATE TABLE agent.memory_consents (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  granted_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version >= 1),
  PRIMARY KEY (tenant_id, owner_user_id, recipient)
);
CREATE TABLE agent.task_context_policies (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
  enabled boolean NOT NULL DEFAULT true,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, task_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);
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

CREATE TABLE agent.memory_consent_revisions (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  revision bigint NOT NULL CHECK (revision >= 1),
  change_kind text NOT NULL CHECK (change_kind IN ('granted','revoked')),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  actor_user_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, owner_user_id, recipient, revision),
  FOREIGN KEY (tenant_id, owner_user_id, recipient)
    REFERENCES agent.memory_consents(tenant_id, owner_user_id, recipient)
);
ALTER TABLE agent.memory_consent_revisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_tenant ON agent.memory_consent_revisions TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY service_all ON agent.memory_consent_revisions TO aiagent_ops USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON agent.memory_consent_revisions TO aiagent_api;
GRANT SELECT ON agent.memory_consent_revisions TO aiagent_ops;
CREATE TRIGGER memory_consent_revision_append_only BEFORE UPDATE OR DELETE
  ON agent.memory_consent_revisions FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();

-- ---------------------------------------------------------------- Claim-bound & metadata functions
CREATE FUNCTION agent.memory_artifact_usable(p_tenant uuid, p_owner uuid, p_task uuid, p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT p_tenant::text = current_setting('aiagent.memory_tenant', true)
     AND p_owner::text = current_setting('aiagent.memory_owner', true)
     AND p_task::text = current_setting('aiagent.memory_task', true)
     AND EXISTS (SELECT 1 FROM agent.context_artifacts a
       WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task AND a.id = p_id
         AND a.kind = 'raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp())
$$;
REVOKE ALL ON FUNCTION agent.memory_artifact_usable(uuid, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_artifact_usable(uuid, uuid, uuid, uuid) TO aiagent_api;

CREATE FUNCTION agent.context_artifact_put(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_kind text, p_sha text, p_length integer, p_nonce bytea, p_ciphertext bytea,
  p_expires_at timestamptz, p_key_id text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  INSERT INTO agent.context_artifacts(tenant_id, id, task_id, run_id, owner_user_id, kind, content_sha256,
    byte_length, nonce, ciphertext, expires_at, key_id)
  VALUES (p_tenant, p_id, p_task, p_run, p_owner, p_kind, p_sha, p_length, p_nonce, p_ciphertext, p_expires_at, p_key_id);
  RETURN p_id;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer,
  bytea, bytea, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer,
  bytea, bytea, timestamptz, text) TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_put_user(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_id uuid, p_kind text, p_sha text, p_length integer,
  p_nonce bytea, p_ciphertext bytea, p_expires_at timestamptz, p_key_id text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  PERFORM 1 FROM agent.tasks t
   WHERE t.tenant_id = p_tenant AND t.id = p_task AND t.owner_user_id = p_owner
     AND t.active_run_id = p_run AND agent.scope_active(p_tenant, p_owner, t.workspace_id);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'memory scope denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  INSERT INTO agent.context_artifacts(tenant_id, id, task_id, run_id, owner_user_id, kind, content_sha256,
    byte_length, nonce, ciphertext, expires_at, key_id)
  VALUES (p_tenant, p_id, p_task, p_run, p_owner, p_kind, p_sha, p_length, p_nonce, p_ciphertext,
          p_expires_at, p_key_id);
  RETURN p_id;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea, bytea,
  timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea,
  bytea, timestamptz, text) TO aiagent_model_gateway, aiagent_api;

CREATE FUNCTION agent.context_artifact_fetch(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid, p_kind text
) RETURNS TABLE(run_id uuid, content_sha256 char(64), byte_length integer, nonce bytea, ciphertext bytea, key_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT a.run_id, a.content_sha256, a.byte_length, a.nonce, a.ciphertext, a.key_id
      FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.kind = p_kind AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp();
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_present(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_kind text, p_sha text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (
    SELECT 1 FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.kind = p_kind AND a.content_sha256 = p_sha
       AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_live(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (
    SELECT 1 FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_revoke(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id) VALUES (p_tenant, p_owner, p_task)
    ON CONFLICT DO NOTHING;
  PERFORM 1 FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task FOR UPDATE;
  UPDATE agent.context_artifacts SET revoked_at = clock_timestamp(), ciphertext = NULL
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task
     AND id = p_id AND revoked_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1
     WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  END IF;
  RETURN n > 0;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_restore_ready(p_tenant uuid, p_owner uuid, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT coalesce((SELECT s.restore_incarnation = 1 OR EXISTS (
      SELECT 1 FROM agent.memory_restore_state m WHERE m.restore_incarnation = s.restore_incarnation)
    FROM agent.schema_control s), false)
$$;
REVOKE ALL ON FUNCTION agent.memory_restore_ready(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_restore_ready(uuid, uuid, uuid) TO
  aiagent_api, aiagent_model_gateway, aiagent_ops;

CREATE FUNCTION agent.memory_artifact_ok(p_tenant uuid, p_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (SELECT 1 FROM agent.context_artifacts a
    WHERE a.tenant_id = p_tenant AND a.id = p_id AND a.kind = 'raw'
      AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp())
$$;
REVOKE ALL ON FUNCTION agent.memory_artifact_ok(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_artifact_ok(uuid, uuid) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_epoch(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO e FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  IF e IS NULL THEN
    RAISE EXCEPTION 'memory policy unavailable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN e;
END $$;
REVOKE ALL ON FUNCTION agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_epoch_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_epoch bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE actual bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO actual FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task FOR UPDATE;
  RETURN coalesce(actual = p_epoch, false);
END $$;
REVOKE ALL ON FUNCTION agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_live_rows(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(id uuid, source_artifact_id uuid, projection_artifact_id uuid, recipient text,
                consent_basis text, observed_at timestamptz, expires_at timestamptz, revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT m.id, m.source_artifact_id, m.projection_artifact_id, m.recipient, m.consent_basis,
           m.observed_at, m.expires_at, m.revision
      FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id)
     ORDER BY m.created_at DESC, m.id;
END $$;
REVOKE ALL ON FUNCTION agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_row_current(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_revision bigint, p_recipient text, p_epoch bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE actual bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO actual FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  RETURN coalesce(actual = p_epoch, false) AND EXISTS (
    SELECT 1 FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.id = p_id AND m.revision = p_revision AND m.recipient = p_recipient
       AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id));
END $$;
REVOKE ALL ON FUNCTION agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_row(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS TABLE(id uuid, source_artifact_id uuid, projection_artifact_id uuid, recipient text,
                consent_basis text, observed_at timestamptz, expires_at timestamptz, revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT m.id, m.source_artifact_id, m.projection_artifact_id, m.recipient, m.consent_basis,
           m.observed_at, m.expires_at, m.revision
      FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.id = p_id AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id);
END $$;
REVOKE ALL ON FUNCTION agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_source_usable(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (SELECT 1 FROM agent.context_artifacts a
    WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
      AND a.id = p_id AND a.kind = 'raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_dispatch_record(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_head bigint, p_attempt uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM agent.model_attempts a
     WHERE a.tenant_id = p_tenant AND a.task_id = p_task AND a.run_id = p_run
       AND a.id = p_attempt AND a.run_lease_epoch = p_lease AND a.admitted
  ) THEN
    RETURN false;
  END IF;
  INSERT INTO agent.context_dispatches(tenant_id, owner_user_id, task_id, run_id, head_revision, model_attempt_id,
    status_code) VALUES (p_tenant, p_owner, p_task, p_run, p_head, p_attempt, 'ADMITTED')
  ON CONFLICT DO NOTHING;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_dispatch_finish(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_head bigint, p_attempt uuid, p_status text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE a record; n integer;
BEGIN
  IF p_status NOT IN ('COMPLETED','UNKNOWN') THEN
    RAISE EXCEPTION 'invalid dispatch status' USING ERRCODE = 'check_violation';
  END IF;
  SELECT d.model_attempt_id, ma.request_identity
    INTO a
    FROM agent.context_dispatches d
    JOIN agent.model_attempts ma ON ma.tenant_id = d.tenant_id AND ma.id = d.model_attempt_id
   WHERE d.tenant_id = p_tenant AND d.owner_user_id = p_owner AND d.task_id = p_task AND d.run_id = p_run
     AND d.head_revision = p_head AND d.model_attempt_id = p_attempt AND d.status_code = 'ADMITTED'
     AND ma.task_id = p_task AND ma.run_id = p_run AND ma.run_lease_epoch = p_lease
   FOR UPDATE OF d;
  IF NOT FOUND THEN RETURN false; END IF;
  IF p_status = 'COMPLETED' THEN
    IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
       OR NOT EXISTS (
         SELECT 1 FROM agent.run_checkpoints cp
          WHERE cp.tenant_id = p_tenant AND cp.task_id = p_task AND cp.run_id = p_run
            AND cp.stream = 'main' AND cp.entry_kind = 'assistant'
            AND cp.request_key = a.request_identity AND cp.checkpoint_revision > p_head
       ) THEN
      RETURN false;
    END IF;
  END IF;
  UPDATE agent.context_dispatches SET status_code = p_status
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task AND run_id = p_run
     AND head_revision = p_head AND model_attempt_id = p_attempt AND status_code = 'ADMITTED';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;
END $$;
REVOKE ALL ON FUNCTION agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text)
  TO aiagent_model_gateway;

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

CREATE FUNCTION agent.context_policy_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model
      FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner
     FOR SHARE OF p;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_consent_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_recipient text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE granted boolean;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT true INTO granted FROM agent.memory_consents m
   WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.recipient = p_recipient
     AND m.revoked_at IS NULL
   FOR SHARE OF m;
  RETURN coalesce(granted, false);
END $$;
REVOKE ALL ON FUNCTION agent.memory_consent_lock(uuid, uuid, uuid, uuid, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_consent_lock(uuid, uuid, uuid, uuid, bigint, text, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_tombstone_coverage_read(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(covered_revocation_generation bigint, chain_head char(64))
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT c.covered_revocation_generation, c.chain_head
      FROM agent.memory_tombstone_coverage c WHERE c.singleton;
END $$;
REVOKE ALL ON FUNCTION agent.memory_tombstone_coverage_read(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_tombstone_coverage_read(uuid, uuid, uuid, uuid, bigint, text)
  TO aiagent_model_gateway, aiagent_ops;

-- ---------------------------------------------------------------- Privileged retention GC functions
CREATE FUNCTION agent.gc_expired_context_artifacts() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE r record; n integer; total integer := 0;
BEGIN
  FOR r IN SELECT DISTINCT tenant_id, owner_user_id, task_id FROM agent.context_artifacts
    WHERE revoked_at IS NULL AND expires_at <= clock_timestamp() ORDER BY tenant_id, owner_user_id, task_id LOOP
    INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id) VALUES (r.tenant_id, r.owner_user_id, r.task_id)
      ON CONFLICT DO NOTHING;
    PERFORM 1 FROM agent.memory_scope_epochs WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id
      AND task_id = r.task_id FOR UPDATE;
    UPDATE agent.context_artifacts SET revoked_at = clock_timestamp(), ciphertext = NULL
     WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id
       AND revoked_at IS NULL AND expires_at <= clock_timestamp();
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
      UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1
       WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id;
      total := total + n;
    END IF;
  END LOOP;
  RETURN total;
END $$;
REVOKE ALL ON FUNCTION agent.gc_expired_context_artifacts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.gc_expired_context_artifacts() TO aiagent_ops;

CREATE FUNCTION agent.gc_orphan_context_artifacts(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE r record; n integer; total integer := 0;
BEGIN
  FOR r IN
    SELECT DISTINCT a.tenant_id, a.owner_user_id, a.task_id
      FROM agent.context_artifacts a
      JOIN agent.runs ru ON ru.tenant_id = a.tenant_id AND ru.id = a.run_id
     WHERE a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
       AND a.created_at < clock_timestamp() - older_than
       AND ru.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
       AND NOT EXISTS (SELECT 1 FROM agent.memory_records m
             WHERE m.tenant_id = a.tenant_id AND m.revoked_at IS NULL
               AND m.expires_at > clock_timestamp()
               AND a.id IN (m.source_artifact_id, m.projection_artifact_id))
       AND NOT EXISTS (SELECT 1 FROM agent.run_checkpoints cp
             WHERE cp.tenant_id = a.tenant_id AND cp.run_id = a.run_id AND cp.entry_kind = 'summary'
               AND (cp.checkpoint_json->>'payloadRef' = a.id::text
                    OR cp.checkpoint_json->>'summaryRef' = a.id::text
                    OR cp.checkpoint_json->'rawRefs' ? a.id::text))
     ORDER BY a.tenant_id, a.owner_user_id, a.task_id LOOP
    INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id)
      VALUES (r.tenant_id, r.owner_user_id, r.task_id) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM agent.memory_scope_epochs
     WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id FOR UPDATE;
    UPDATE agent.context_artifacts a SET revoked_at = clock_timestamp(), ciphertext = NULL
      FROM agent.runs ru
     WHERE a.tenant_id = r.tenant_id AND a.owner_user_id = r.owner_user_id AND a.task_id = r.task_id
       AND ru.tenant_id = a.tenant_id AND ru.id = a.run_id
       AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
       AND a.created_at < clock_timestamp() - older_than
       AND ru.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
       AND NOT EXISTS (SELECT 1 FROM agent.memory_records m
             WHERE m.tenant_id = a.tenant_id AND m.revoked_at IS NULL
               AND m.expires_at > clock_timestamp()
               AND a.id IN (m.source_artifact_id, m.projection_artifact_id))
       AND NOT EXISTS (SELECT 1 FROM agent.run_checkpoints cp
             WHERE cp.tenant_id = a.tenant_id AND cp.run_id = a.run_id AND cp.entry_kind = 'summary'
               AND (cp.checkpoint_json->>'payloadRef' = a.id::text
                    OR cp.checkpoint_json->>'summaryRef' = a.id::text
                    OR cp.checkpoint_json->'rawRefs' ? a.id::text));
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
      UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1
       WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id;
      total := total + n;
    END IF;
  END LOOP;
  RETURN total;
END $$;
REVOKE ALL ON FUNCTION agent.gc_orphan_context_artifacts(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.gc_orphan_context_artifacts(interval) TO aiagent_ops;
