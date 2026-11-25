-- Canonical B2 execution authority metadata. No Artifact bytes or service endpoint
-- is created here. Operator-provisioned bindings/recipes are distinct from
-- model proposals and are inaccessible to runtime/API writers.

-- Scope locks serialize publication/registration with membership and workspace
-- disablement. Only the trusted Tool Gateway may request this narrow lock.
CREATE FUNCTION agent.execution_lock_scope(p_tenant uuid, p_owner uuid, p_workspace uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  PERFORM 1 FROM agent.tenants WHERE id = p_tenant AND disabled_at IS NULL FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SCOPE_REVOKED' USING ERRCODE = 'insufficient_privilege'; END IF;
  PERFORM 1 FROM agent.users WHERE id = p_owner AND disabled_at IS NULL FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SCOPE_REVOKED' USING ERRCODE = 'insufficient_privilege'; END IF;
  PERFORM 1 FROM agent.tenant_memberships
    WHERE tenant_id = p_tenant AND user_id = p_owner AND disabled_at IS NULL FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SCOPE_REVOKED' USING ERRCODE = 'insufficient_privilege'; END IF;
  PERFORM 1 FROM agent.workspaces
    WHERE tenant_id = p_tenant AND id = p_workspace AND owner_user_id = p_owner AND disabled_at IS NULL FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SCOPE_REVOKED' USING ERRCODE = 'insufficient_privilege'; END IF;
END $$;
REVOKE ALL ON FUNCTION agent.execution_lock_scope(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_lock_scope(uuid, uuid, uuid) TO aiagent_tool_gateway;

CREATE TABLE agent.repository_bindings (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  repository_ref text NOT NULL CHECK (length(repository_ref) BETWEEN 3 AND 256
    AND repository_ref ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
    AND split_part(repository_ref, '/', 1) NOT IN ('.', '..')
    AND split_part(repository_ref, '/', 2) NOT IN ('.', '..')),
  approved_commit text NOT NULL CHECK (approved_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  workspace_revision bigint NOT NULL CHECK (workspace_revision >= 1),
  generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
  lock_token smallint NOT NULL DEFAULT 0 CHECK (lock_token = 0),
  approval_ref text NOT NULL CHECK (length(approval_ref) BETWEEN 1 AND 256),
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, workspace_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id)
);

CREATE FUNCTION agent.repository_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id <> OLD.tenant_id OR NEW.id <> OLD.id OR NEW.owner_user_id <> OLD.owner_user_id
     OR NEW.workspace_id <> OLD.workspace_id OR NEW.repository_ref <> OLD.repository_ref
     OR NEW.generation <> OLD.generation + 1
     OR (NOT OLD.active AND NEW.active)
     OR NEW.workspace_revision < OLD.workspace_revision
     OR (NEW.approved_commit <> OLD.approved_commit AND NEW.workspace_revision <= OLD.workspace_revision)
  THEN
    RAISE EXCEPTION 'INVALID_BINDING_UPDATE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER repository_binding_update_guard BEFORE UPDATE ON agent.repository_bindings
  FOR EACH ROW EXECUTE FUNCTION agent.repository_binding_guard();

CREATE TABLE agent.workspace_snapshot_attestations (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  binding_id uuid NOT NULL,
  binding_generation bigint NOT NULL CHECK (binding_generation >= 1),
  workspace_revision bigint NOT NULL CHECK (workspace_revision >= 1),
  commit_sha text NOT NULL CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  tree_sha text NOT NULL CHECK (tree_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$' AND length(tree_sha) = length(commit_sha)),
  manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  source_bytes bigint NOT NULL CHECK (source_bytes BETWEEN 1 AND 134217728),
  hash_strength text NOT NULL CHECK (hash_strength IN ('LEGACY_SHA1', 'SHA256')),
  CHECK ((length(commit_sha) = 40 AND hash_strength = 'LEGACY_SHA1') OR
         (length(commit_sha) = 64 AND hash_strength = 'SHA256')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, workspace_id, id),
  UNIQUE (tenant_id, binding_id, workspace_revision),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id),
  FOREIGN KEY (tenant_id, workspace_id, binding_id) REFERENCES agent.repository_bindings(tenant_id, workspace_id, id)
);

CREATE TABLE agent.workspace_snapshot_heads (
  tenant_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  PRIMARY KEY (tenant_id, workspace_id),
  FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
    REFERENCES agent.workspace_snapshot_attestations(tenant_id, workspace_id, id)
);

CREATE TABLE agent.execution_recipe_approvals (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  cycle_id text NOT NULL,
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  plan_sha256 char(64) NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
  plan_json jsonb NOT NULL CHECK (jsonb_typeof(plan_json) = 'object'),
  kind text NOT NULL CHECK (kind IN ('build', 'test', 'static-check', 'index')),
  test_id text CHECK (test_id IS NULL OR length(test_id) BETWEEN 1 AND 256),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  template_sha256 char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
  recipe_sha256 char(64) NOT NULL CHECK (recipe_sha256 ~ '^[0-9a-f]{64}$'),
  toolchain_sha256 char(64) NOT NULL CHECK (toolchain_sha256 ~ '^[0-9a-f]{64}$'),
  image_digest text NOT NULL CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
  approval_ref text NOT NULL CHECK (length(approval_ref) BETWEEN 1 AND 256),
  active boolean NOT NULL DEFAULT true,
  lock_token smallint NOT NULL DEFAULT 0 CHECK (lock_token = 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  CHECK ((kind = 'test') = (test_id IS NOT NULL))
);

CREATE FUNCTION agent.execution_recipe_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.tenant_id, NEW.id, NEW.cycle_id, NEW.goal_revision, NEW.plan_sha256, NEW.plan_json, NEW.kind, NEW.test_id,
         NEW.request_sha256, NEW.template_sha256, NEW.recipe_sha256, NEW.toolchain_sha256, NEW.image_digest, NEW.approval_ref, NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id, OLD.id, OLD.cycle_id, OLD.goal_revision, OLD.plan_sha256, OLD.plan_json, OLD.kind, OLD.test_id,
         OLD.request_sha256, OLD.template_sha256, OLD.recipe_sha256, OLD.toolchain_sha256, OLD.image_digest, OLD.approval_ref, OLD.created_at)
     OR (NOT OLD.active AND NEW.active)
  THEN
    RAISE EXCEPTION 'INVALID_RECIPE_UPDATE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_recipe_update_guard BEFORE UPDATE ON agent.execution_recipe_approvals
  FOR EACH ROW EXECUTE FUNCTION agent.execution_recipe_guard();

CREATE TABLE agent.execution_instances (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  lease_epoch bigint NOT NULL CHECK (lease_epoch >= 0),
  worker_ref text NOT NULL CHECK (length(worker_ref) BETWEEN 1 AND 256),
  restore_incarnation bigint NOT NULL CHECK (restore_incarnation >= 1),
  tool_intent_id uuid NOT NULL,
  tool_attempt_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  recipe_id uuid NOT NULL,
  binding_sha256 char(64) NOT NULL CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
  lock_token smallint NOT NULL DEFAULT 0 CHECK (lock_token = 0),
  state text NOT NULL CHECK (state IN ('PREPARED', 'RUNNING', 'FINISHED', 'PUBLISHED', 'REVOKED', 'UNKNOWN')),
  capture_sha256 char(64) CHECK (capture_sha256 IS NULL OR capture_sha256 ~ '^[0-9a-f]{64}$'),
  capture_bytes integer CHECK (capture_bytes IS NULL OR capture_bytes BETWEEN 1 AND 4194304),
  capture_proof_sha256 char(64) CHECK (capture_proof_sha256 IS NULL OR capture_proof_sha256 ~ '^[0-9a-f]{64}$'),
  artifact_sha256 char(64) CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
  artifact_bytes integer CHECK (artifact_bytes IS NULL OR artifact_bytes BETWEEN 1 AND 4194304),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, tool_attempt_id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id),
  FOREIGN KEY (tenant_id, tool_intent_id) REFERENCES agent.tool_intents(tenant_id, id),
  FOREIGN KEY (tenant_id, tool_attempt_id) REFERENCES agent.tool_attempts(tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, snapshot_id) REFERENCES agent.workspace_snapshot_attestations(tenant_id, workspace_id, id),
  FOREIGN KEY (tenant_id, recipe_id) REFERENCES agent.execution_recipe_approvals(tenant_id, id),
  CHECK ((capture_sha256 IS NULL) = (capture_bytes IS NULL)),
  CHECK ((capture_sha256 IS NULL) = (capture_proof_sha256 IS NULL)),
  CHECK (state NOT IN ('FINISHED','PUBLISHED') OR capture_sha256 IS NOT NULL),
  CHECK ((artifact_sha256 IS NULL) = (artifact_bytes IS NULL)),
  CHECK (id <> tool_attempt_id),
  CHECK ((state = 'PUBLISHED') = (artifact_sha256 IS NOT NULL))
);

CREATE FUNCTION agent.execution_instance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'PREPARED' OR NEW.capture_sha256 IS NOT NULL OR NEW.artifact_sha256 IS NOT NULL OR NEW.artifact_bytes IS NOT NULL THEN
      RAISE EXCEPTION 'INVALID_EXECUTION_INSERT' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.tenant_id, NEW.id, NEW.owner_user_id, NEW.workspace_id, NEW.task_id, NEW.run_id, NEW.goal_revision,
         NEW.lease_epoch, NEW.worker_ref, NEW.restore_incarnation, NEW.tool_intent_id, NEW.tool_attempt_id,
         NEW.snapshot_id, NEW.recipe_id, NEW.binding_sha256, NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id, OLD.id, OLD.owner_user_id, OLD.workspace_id, OLD.task_id, OLD.run_id, OLD.goal_revision,
         OLD.lease_epoch, OLD.worker_ref, OLD.restore_incarnation, OLD.tool_intent_id, OLD.tool_attempt_id,
         OLD.snapshot_id, OLD.recipe_id, OLD.binding_sha256, OLD.created_at)
     OR (OLD.state = 'PREPARED' AND NEW.state NOT IN ('PREPARED','RUNNING','REVOKED','UNKNOWN'))
     OR (OLD.state = 'RUNNING' AND NEW.state NOT IN ('RUNNING','FINISHED','REVOKED','UNKNOWN'))
     OR (OLD.state = 'FINISHED' AND NEW.state NOT IN ('FINISHED','PUBLISHED','REVOKED','UNKNOWN'))
     OR (OLD.state IN ('PUBLISHED','REVOKED','UNKNOWN') AND NEW.state <> OLD.state)
     OR (ROW(NEW.capture_sha256,NEW.capture_bytes,NEW.capture_proof_sha256) IS DISTINCT FROM
             ROW(OLD.capture_sha256,OLD.capture_bytes,OLD.capture_proof_sha256)
         AND NOT (OLD.state='RUNNING' AND NEW.state='FINISHED' AND OLD.capture_sha256 IS NULL))
     OR (OLD.state = 'PUBLISHED' AND ROW(NEW.artifact_sha256,NEW.artifact_bytes) IS DISTINCT FROM
                                     ROW(OLD.artifact_sha256,OLD.artifact_bytes))
  THEN
    RAISE EXCEPTION 'INVALID_EXECUTION_UPDATE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER execution_instance_update_guard BEFORE INSERT OR UPDATE ON agent.execution_instances
  FOR EACH ROW EXECUTE FUNCTION agent.execution_instance_guard();

ALTER TABLE agent.repository_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.workspace_snapshot_attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.workspace_snapshot_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.execution_recipe_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.execution_instances ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.repository_bindings, agent.workspace_snapshot_attestations, agent.workspace_snapshot_heads,
  agent.execution_recipe_approvals, agent.execution_instances FROM PUBLIC;

CREATE POLICY repo_binding_ops ON agent.repository_bindings TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY snapshot_ops ON agent.workspace_snapshot_attestations TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY snapshot_heads_ops ON agent.workspace_snapshot_heads TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY execution_recipes_ops ON agent.execution_recipe_approvals TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY executions_ops ON agent.execution_instances TO aiagent_ops USING (true) WITH CHECK (true);

CREATE POLICY repo_binding_gateway ON agent.repository_bindings TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY snapshot_gateway ON agent.workspace_snapshot_attestations TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY snapshot_heads_gateway ON agent.workspace_snapshot_heads TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY execution_recipes_gateway ON agent.execution_recipe_approvals TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY executions_gateway ON agent.execution_instances TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON agent.repository_bindings, agent.execution_recipe_approvals TO aiagent_ops;
GRANT SELECT ON agent.workspace_snapshot_attestations, agent.workspace_snapshot_heads, agent.execution_instances TO aiagent_ops;
-- Row locks require UPDATE privilege on at least one column. A constrained
-- dummy column permits locking but cannot change approval-bearing fields.
GRANT SELECT ON agent.repository_bindings, agent.execution_recipe_approvals TO aiagent_tool_gateway;
GRANT UPDATE (lock_token) ON agent.repository_bindings, agent.execution_recipe_approvals TO aiagent_tool_gateway;
GRANT SELECT, INSERT ON agent.workspace_snapshot_attestations TO aiagent_tool_gateway;
GRANT SELECT, INSERT, UPDATE ON agent.workspace_snapshot_heads TO aiagent_tool_gateway;
GRANT SELECT, INSERT ON agent.execution_instances TO aiagent_tool_gateway;
GRANT UPDATE (lock_token) ON agent.execution_instances TO aiagent_tool_gateway;

-- Encrypted execution Artifacts are separate from Context raw/summary and are
-- never exposed to model dispatch. The Tool Gateway has only narrow function
-- access; no process role receives direct table privileges.
CREATE TABLE agent.execution_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  execution_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  plaintext_sha256 char(64) NOT NULL CHECK (plaintext_sha256 ~ '^[0-9a-f]{64}$'),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 4194304),
  coverage text NOT NULL CHECK (coverage IN ('VALIDATED', 'PARTIAL')),
  origin_trust text NOT NULL DEFAULT 'UNVERIFIED' CHECK (origin_trust = 'UNVERIFIED'),
  key_id text NOT NULL CHECK (key_id ~ '^[A-Za-z0-9._-]{1,64}$'),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) = byte_length + 16),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, execution_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES agent.execution_instances(tenant_id, id),
  CHECK (expires_at > created_at)
);
ALTER TABLE agent.execution_artifacts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.execution_artifacts FROM PUBLIC;

-- The lock order is scope -> task -> run -> workspace advisory lock -> head /
-- binding -> recipe -> cycle -> attempt / intent -> execution. The caller must
-- still authenticate the Supervisor result before marking FINISHED. No public
-- receipt/FINISHED transition is supplied by this migration.
CREATE FUNCTION agent.execution_artifact_lock(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid
) RETURNS agent.execution_instances
LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  t agent.tasks%ROWTYPE;
  r agent.runs%ROWTYPE;
  hint agent.execution_instances%ROWTYPE;
  e agent.execution_instances%ROWTYPE;
  snap agent.workspace_snapshot_attestations%ROWTYPE;
  binding agent.repository_bindings%ROWTYPE;
  recipe agent.execution_recipe_approvals%ROWTYPE;
  cycle agent.dev_cycles%ROWTYPE;
  attempt agent.tool_attempts%ROWTYPE;
  intent agent.tool_intents%ROWTYPE;
  current_snapshot uuid;
  incarnation bigint;
BEGIN
  PERFORM agent.execution_lock_scope(p_tenant, p_owner, p_workspace);
  SELECT * INTO t FROM agent.tasks WHERE tenant_id=p_tenant AND id=p_task FOR UPDATE;
  SELECT * INTO r FROM agent.runs WHERE tenant_id=p_tenant AND id=p_run AND task_id=p_task FOR UPDATE;
  SELECT restore_incarnation INTO incarnation FROM agent.schema_control WHERE singleton;
  IF t.id IS NULL OR r.id IS NULL OR incarnation IS NULL OR t.owner_user_id <> p_owner
     OR t.workspace_id <> p_workspace OR t.active_run_id <> p_run OR t.goal_revision <> p_goal
     OR r.lease_epoch <> p_lease OR r.lease_owner_ref IS DISTINCT FROM p_worker
     OR r.lease_expires_at IS NULL OR r.lease_expires_at <= clock_timestamp()
     OR r.restore_incarnation <> p_restore OR incarnation <> p_restore
     OR r.cancel_requested OR r.pause_requested OR r.status_code NOT IN ('CLAIMED','RUNNING')
  THEN RAISE EXCEPTION 'STALE_EXECUTION' USING ERRCODE = 'insufficient_privilege'; END IF;
  SELECT * INTO hint FROM agent.execution_instances WHERE tenant_id=p_tenant AND id=p_execution;
  IF hint.id IS NULL THEN RAISE EXCEPTION 'EXECUTION_UNAVAILABLE' USING ERRCODE = 'insufficient_privilege'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('execution-workspace:' || p_tenant::text || ':' || p_workspace::text, 0));
  SELECT h.snapshot_id INTO current_snapshot FROM agent.workspace_snapshot_heads h
    WHERE h.tenant_id=p_tenant AND h.workspace_id=p_workspace FOR UPDATE;
  SELECT * INTO snap FROM agent.workspace_snapshot_attestations
    WHERE tenant_id=p_tenant AND id=hint.snapshot_id AND workspace_id=p_workspace;
  SELECT * INTO binding FROM agent.repository_bindings
    WHERE tenant_id=p_tenant AND id=snap.binding_id FOR UPDATE;
  SELECT * INTO recipe FROM agent.execution_recipe_approvals
    WHERE tenant_id=p_tenant AND id=hint.recipe_id FOR UPDATE;
  SELECT * INTO cycle FROM agent.dev_cycles WHERE tenant_id=p_tenant AND id=recipe.cycle_id FOR UPDATE;
  SELECT a.* INTO attempt FROM agent.tool_attempts a
    JOIN agent.tool_intents i ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id
    WHERE a.tenant_id=p_tenant AND a.id=hint.tool_attempt_id FOR UPDATE OF a, i;
  SELECT * INTO intent FROM agent.tool_intents
    WHERE tenant_id=p_tenant AND id=attempt.tool_intent_id;
  SELECT * INTO e FROM agent.execution_instances WHERE tenant_id=p_tenant AND id=p_execution FOR UPDATE;
  IF e.id IS NULL OR current_snapshot IS DISTINCT FROM e.snapshot_id OR snap.id IS NULL OR binding.id IS NULL
     OR NOT binding.active OR binding.generation <> snap.binding_generation
     OR binding.workspace_revision <> snap.workspace_revision OR binding.approved_commit <> snap.commit_sha
     OR snap.owner_user_id <> p_owner OR recipe.id IS NULL OR NOT recipe.active OR recipe.goal_revision <> p_goal
     OR cycle.id IS NULL OR cycle.owner_user_id <> p_owner OR cycle.workspace_id <> p_workspace
     OR cycle.task_id <> p_task OR cycle.run_id <> p_run OR cycle.cycle_json->'plan' IS DISTINCT FROM recipe.plan_json
     OR jsonb_typeof(recipe.plan_json->'required_tests') IS DISTINCT FROM 'array'
     OR (recipe.kind = 'test' AND NOT ((recipe.plan_json->'required_tests') ? recipe.test_id))
     OR attempt.id IS NULL OR attempt.tool_intent_id <> e.tool_intent_id OR attempt.id <> e.tool_attempt_id
     OR attempt.run_lease_epoch <> p_lease OR attempt.worker_ref <> p_worker
     OR attempt.status_code NOT IN ('SENDING','CONFIRMED') OR intent.status_code NOT IN ('SENDING','CONFIRMED')
     OR intent.id IS NULL OR intent.task_id <> p_task OR intent.run_id <> p_run OR intent.goal_revision <> p_goal
     OR intent.tool_key <> 'sandbox_exec' OR intent.request_sha256 <> recipe.request_sha256
     OR e.owner_user_id <> p_owner OR e.workspace_id <> p_workspace OR e.task_id <> p_task OR e.run_id <> p_run
     OR e.goal_revision <> p_goal OR e.lease_epoch <> p_lease OR e.worker_ref <> p_worker
     OR e.restore_incarnation <> p_restore OR e.snapshot_id <> hint.snapshot_id OR e.recipe_id <> hint.recipe_id
  THEN RAISE EXCEPTION 'EXECUTION_BINDING_STALE' USING ERRCODE = 'insufficient_privilege'; END IF;
  RETURN e;
END $$;
REVOKE ALL ON FUNCTION agent.execution_artifact_lock(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;

-- These functions are callable only by the trusted Gateway owner. The Gateway
-- must authenticate the Supervisor receipt before invoking finish.
CREATE FUNCTION agent.execution_start(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid, p_binding text
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE;
BEGIN
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.state <> 'PREPARED' OR e.binding_sha256 <> p_binding OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id
    WHERE a.tenant_id=p_tenant AND a.id=e.tool_attempt_id
      AND a.status_code='SENDING' AND i.status_code='SENDING'
  ) THEN
    RAISE EXCEPTION 'EXECUTION_NOT_PREPARED' USING ERRCODE='check_violation';
  END IF;
  UPDATE agent.execution_instances SET state='RUNNING' WHERE tenant_id=p_tenant AND id=p_execution;
END $$;
REVOKE ALL ON FUNCTION agent.execution_start(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_start(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text) TO aiagent_tool_gateway;

CREATE FUNCTION agent.execution_finish(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid,
  p_binding text, p_sha text, p_length integer, p_proof_sha text
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE;
BEGIN
  IF p_sha !~ '^[0-9a-f]{64}$' OR p_proof_sha !~ '^[0-9a-f]{64}$' OR p_length NOT BETWEEN 1 AND 4194304 THEN
    RAISE EXCEPTION 'INVALID_CAPTURE' USING ERRCODE='check_violation';
  END IF;
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.binding_sha256 <> p_binding THEN RAISE EXCEPTION 'EXECUTION_BINDING_STALE' USING ERRCODE='insufficient_privilege'; END IF;
  IF e.state='FINISHED' AND e.capture_sha256=p_sha AND e.capture_bytes=p_length AND e.capture_proof_sha256=p_proof_sha THEN RETURN; END IF;
  IF e.state <> 'RUNNING' THEN RAISE EXCEPTION 'EXECUTION_NOT_RUNNING' USING ERRCODE='check_violation'; END IF;
  UPDATE agent.execution_instances SET state='FINISHED',capture_sha256=p_sha,capture_bytes=p_length,capture_proof_sha256=p_proof_sha
    WHERE tenant_id=p_tenant AND id=p_execution;
END $$;
REVOKE ALL ON FUNCTION agent.execution_finish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,text,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_finish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,text,integer,text) TO aiagent_tool_gateway;

CREATE FUNCTION agent.execution_artifact_publish(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid,
  p_artifact uuid, p_sha text, p_length integer, p_coverage text, p_key_id text, p_nonce bytea, p_ciphertext bytea, p_expires timestamptz, p_proof_sha text
) RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE; existing agent.execution_artifacts%ROWTYPE;
BEGIN
  IF p_sha !~ '^[0-9a-f]{64}$' OR p_proof_sha !~ '^[0-9a-f]{64}$' OR p_length NOT BETWEEN 1 AND 4194304 OR p_coverage NOT IN ('VALIDATED','PARTIAL')
     OR p_key_id !~ '^[A-Za-z0-9._-]{1,64}$' OR octet_length(p_nonce) <> 12
     OR octet_length(p_ciphertext) <> p_length + 16 OR p_expires <= clock_timestamp()
     OR p_expires > clock_timestamp() + interval '30 days'
  THEN RAISE EXCEPTION 'INVALID_ARTIFACT' USING ERRCODE = 'check_violation'; END IF;
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.state = 'PUBLISHED' THEN
    SELECT * INTO existing FROM agent.execution_artifacts WHERE tenant_id=p_tenant AND execution_id=p_execution;
    IF existing.id IS NOT NULL AND existing.plaintext_sha256=p_sha AND existing.byte_length=p_length AND existing.coverage=p_coverage
       AND e.capture_proof_sha256=p_proof_sha AND existing.revoked_at IS NULL AND existing.expires_at > clock_timestamp()
    THEN RETURN existing.id; END IF;
    RAISE EXCEPTION 'ARTIFACT_CONFLICT' USING ERRCODE = 'check_violation';
  END IF;
  IF e.state <> 'FINISHED' THEN RAISE EXCEPTION 'EXECUTION_NOT_FINISHED' USING ERRCODE = 'check_violation'; END IF;
  IF e.capture_sha256 IS DISTINCT FROM p_sha OR e.capture_bytes IS DISTINCT FROM p_length
     OR e.capture_proof_sha256 IS DISTINCT FROM p_proof_sha THEN
    RAISE EXCEPTION 'CAPTURE_MISMATCH' USING ERRCODE = 'check_violation';
  END IF;
  IF p_coverage = 'VALIDATED' AND EXISTS (SELECT 1 FROM agent.workspace_snapshot_attestations s
    WHERE s.tenant_id=p_tenant AND s.id=e.snapshot_id AND s.hash_strength='LEGACY_SHA1')
  THEN RAISE EXCEPTION 'LEGACY_GIT_REQUIRES_PARTIAL' USING ERRCODE = 'check_violation'; END IF;
  INSERT INTO agent.execution_artifacts(tenant_id,id,execution_id,owner_user_id,plaintext_sha256,byte_length,
                                        coverage,key_id,nonce,ciphertext,expires_at)
    VALUES (p_tenant,p_artifact,p_execution,p_owner,p_sha,p_length,p_coverage,p_key_id,p_nonce,p_ciphertext,p_expires);
  UPDATE agent.execution_instances SET state='PUBLISHED',artifact_sha256=p_sha,artifact_bytes=p_length
    WHERE tenant_id=p_tenant AND id=p_execution;
  RETURN p_artifact;
END $$;
REVOKE ALL ON FUNCTION agent.execution_artifact_publish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,text,integer,text,text,bytea,bytea,timestamptz,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_artifact_publish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,text,integer,text,text,bytea,bytea,timestamptz,text) TO aiagent_tool_gateway;

CREATE FUNCTION agent.execution_artifact_fetch(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid
) RETURNS TABLE(id uuid, plaintext_sha256 char(64), byte_length integer, coverage text, key_id text, nonce bytea, ciphertext bytea)
LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE;
BEGIN
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.state <> 'PUBLISHED' THEN RAISE EXCEPTION 'ARTIFACT_UNAVAILABLE' USING ERRCODE = 'insufficient_privilege'; END IF;
  RETURN QUERY SELECT a.id,a.plaintext_sha256,a.byte_length,a.coverage,a.key_id,a.nonce,a.ciphertext FROM agent.execution_artifacts a
    WHERE a.tenant_id=p_tenant AND a.execution_id=p_execution AND a.owner_user_id=p_owner
      AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
      AND a.plaintext_sha256=e.artifact_sha256 AND a.byte_length=e.artifact_bytes;
END $$;
REVOKE ALL ON FUNCTION agent.execution_artifact_fetch(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_artifact_fetch(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) TO aiagent_tool_gateway;
