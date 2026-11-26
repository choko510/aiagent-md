-- 0029_autonomous_development: Autonomous Software Development application layer (DEV-01..DEV-14).
-- Built on top of 0013..0028 execution & isolated Git CAS authority; never redefines Task, Run,
-- Approval, Tool Intent, Execution Authority, or Git Operation ownership.

CREATE TABLE agent.dev_projects (
  tenant_id uuid NOT NULL,
  id text NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  name text NOT NULL,
  repository text NOT NULL CHECK (repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  default_branch text NOT NULL,
  framework text NOT NULL DEFAULT 'web',
  repository_binding_id uuid,
  github_api_base text,
  preferences_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ARCHIVED')),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, workspace_id, repository, default_branch),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES agent.workspaces(tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, repository_binding_id)
    REFERENCES agent.repository_bindings(tenant_id, workspace_id, id)
);

CREATE INDEX dev_projects_owner_idx ON agent.dev_projects (tenant_id, owner_user_id, created_at DESC);

-- Narrow SECURITY DEFINER authority helper verifying an active operator-provisioned repository_binding
-- without granting raw SELECT on agent.repository_bindings to least-privilege API/Sentinel roles (R2-02).
CREATE FUNCTION agent.verify_dev_repository_binding(
  p_tenant uuid,
  p_owner uuid,
  p_workspace uuid,
  p_binding uuid,
  p_repository text,
  p_branch text DEFAULT NULL
) RETURNS TABLE(
  binding_id uuid,
  binding_generation bigint,
  approved_commit text,
  approved_branch text,
  repository_ref text
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.scope_active(p_tenant, p_owner, p_workspace) THEN
    RETURN;
  END IF;
  RETURN QUERY
  SELECT b.id, b.generation, b.approved_commit, b.approved_branch, b.repository_ref
    FROM agent.repository_bindings b
   WHERE b.tenant_id = p_tenant
     AND b.id = p_binding
     AND b.owner_user_id = p_owner
     AND b.workspace_id = p_workspace
     AND b.repository_ref = p_repository
     AND b.active = true
     AND (p_branch IS NULL OR b.approved_branch IS NULL OR b.approved_branch = p_branch);
END $$;
REVOKE ALL ON FUNCTION agent.verify_dev_repository_binding(uuid, uuid, uuid, uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.verify_dev_repository_binding(uuid, uuid, uuid, uuid, text, text)
  TO aiagent_api, aiagent_tool_gateway, aiagent_sentinel, aiagent_ops;

-- Narrow SECURITY DEFINER authority helper verifying a non-revoked, unexpired reviewer artifact
-- in agent.context_artifacts without exposing ciphertext to least-privilege API/runtime roles (R3-06).
CREATE FUNCTION agent.verify_dev_review_source_artifact(
  p_tenant uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_artifact uuid
) RETURNS TABLE(
  artifact_id uuid,
  kind text,
  content_sha256 char(64),
  byte_length integer
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  RETURN QUERY
  SELECT a.id, a.kind, a.content_sha256, a.byte_length
    FROM agent.context_artifacts a
   WHERE a.tenant_id = p_tenant
     AND a.id = p_artifact
     AND a.owner_user_id = p_owner
     AND a.task_id = p_task
     AND a.run_id = p_run
     AND a.revoked_at IS NULL
     AND a.ciphertext IS NOT NULL
     AND a.expires_at > clock_timestamp();
END $$;
REVOKE ALL ON FUNCTION agent.verify_dev_review_source_artifact(uuid, uuid, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.verify_dev_review_source_artifact(uuid, uuid, uuid, uuid, uuid)
  TO aiagent_api, aiagent_runtime, aiagent_tool_gateway, aiagent_ops;

CREATE TABLE agent.dev_demo_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  candidate_id text NOT NULL,
  commit_sha text NOT NULL CHECK (commit_sha ~ '^[a-f0-9]{40,64}$'),
  viewport text NOT NULL CHECK (viewport ~ '^[0-9]{2,5}x[0-9]{2,5}(@[a-z0-9_-]{1,16})?$'),
  fixture_ref text NOT NULL CHECK (length(fixture_ref) BETWEEN 1 AND 256),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 4194304),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id)
);

CREATE INDEX dev_demo_artifacts_cycle_idx
  ON agent.dev_demo_artifacts (tenant_id, cycle_id, candidate_id, created_at DESC);

CREATE TABLE agent.dev_design_decisions (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  design_revision integer NOT NULL DEFAULT 1 CHECK (design_revision >= 1),
  design_policy text NOT NULL CHECK (design_policy IN ('AUTO_RECOMMEND', 'REQUIRE_USER_SELECTION')),
  demo_acceptance_policy text NOT NULL CHECK (demo_acceptance_policy IN ('REQUIRE_USER_APPROVAL', 'PREAUTHORIZED_BOUNDED_AUTO')),
  candidates_json jsonb NOT NULL,
  recommended_candidate_id text,
  selected_candidate_id text,
  demo_decision text NOT NULL DEFAULT 'PENDING' CHECK (demo_decision IN ('PENDING', 'APPROVED', 'REVISION_REQUESTED', 'REJECTED')),
  demo_render_artifact_id uuid,
  demo_artifact_ref text,
  demo_artifact_sha256 char(64) CHECK (demo_artifact_sha256 IS NULL OR demo_artifact_sha256 ~ '^[a-f0-9]{64}$'),
  demo_viewport text,
  demo_fixture_ref text,
  demo_commit_sha text CHECK (demo_commit_sha IS NULL OR demo_commit_sha ~ '^[a-f0-9]{40,64}$'),
  demo_snapshot_digest char(64) CHECK (demo_snapshot_digest IS NULL OR demo_snapshot_digest ~ '^[a-f0-9]{64}$'),
  feedback text,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, cycle_id, design_revision),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  FOREIGN KEY (tenant_id, demo_render_artifact_id) REFERENCES agent.dev_demo_artifacts(tenant_id, id)
);

CREATE INDEX dev_design_decisions_cycle_idx ON agent.dev_design_decisions (tenant_id, cycle_id, design_revision DESC);

CREATE TABLE agent.dev_pr_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  repository text NOT NULL CHECK (repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  branch text NOT NULL,
  head_sha text NOT NULL CHECK (head_sha ~ '^[a-f0-9]{40,64}$'),
  pr_number integer CHECK (pr_number IS NULL OR pr_number >= 1),
  pr_url text,
  idempotency_key text NOT NULL,
  intent_id uuid,
  git_push_receipt_ref text,
  required_checks_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  effect_status text NOT NULL DEFAULT 'PENDING' CHECK (effect_status IN ('PENDING', 'CONFIRMED', 'EFFECT_UNKNOWN', 'FAILED')),
  ci_state text NOT NULL DEFAULT 'UNKNOWN' CHECK (ci_state IN ('UNKNOWN', 'PENDING', 'PASS', 'FAIL', 'CONFLICT', 'STALE_HEAD', 'ESCALATED_LIMIT_REACHED')),
  repair_attempts integer NOT NULL DEFAULT 0 CHECK (repair_attempts >= 0),
  max_repair_attempts integer NOT NULL DEFAULT 3 CHECK (max_repair_attempts >= 1),
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES agent.tool_intents(tenant_id, id)
);

CREATE UNIQUE INDEX dev_pr_records_intent_uniq ON agent.dev_pr_records (tenant_id, intent_id) WHERE intent_id IS NOT NULL;
CREATE INDEX dev_pr_records_project_idx ON agent.dev_pr_records (tenant_id, project_id, created_at DESC);

CREATE TABLE agent.dev_preview_instances (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  workspace_id uuid NOT NULL,
  commit_sha text NOT NULL CHECK (commit_sha ~ '^[a-f0-9]{40,64}$'),
  target_port integer NOT NULL CHECK (target_port BETWEEN 1024 AND 65535),
  endpoint_ref text NOT NULL CHECK (length(endpoint_ref) BETWEEN 1 AND 256),
  runner_kind text NOT NULL DEFAULT 'sandbox_cell' CHECK (runner_kind IN ('isolated_worker', 'sandbox_cell')),
  process_attestation_sha256 char(64) NOT NULL CHECK (process_attestation_sha256 ~ '^[a-f0-9]{64}$'),
  generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
  launch_nonce text NOT NULL UNIQUE CHECK (length(launch_nonce) BETWEEN 16 AND 128),
  issued_at timestamptz NOT NULL,
  receipt_expires_at timestamptz NOT NULL,
  launch_receipt_hmac char(64) NOT NULL CHECK (launch_receipt_hmac ~ '^[0-9a-f]{64}$'),
  heartbeat_expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING', 'STOPPED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  stopped_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES agent.workspaces(tenant_id, id),
  CHECK (receipt_expires_at > issued_at)
);

CREATE INDEX dev_preview_instances_active_idx
  ON agent.dev_preview_instances (tenant_id, owner_user_id, project_id, cycle_id, target_port)
  WHERE status = 'RUNNING';

CREATE UNIQUE INDEX dev_preview_instances_running_port_uniq
  ON agent.dev_preview_instances (target_port)
  WHERE status = 'RUNNING';

ALTER TABLE agent.preview_leases
  ADD COLUMN IF NOT EXISTS cycle_id text,
  ADD COLUMN IF NOT EXISTS preview_instance_id uuid,
  ADD COLUMN IF NOT EXISTS instance_generation bigint NOT NULL DEFAULT 1;

CREATE TABLE agent.dev_review_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  source_artifact_id uuid NOT NULL,
  reviewer_role text NOT NULL CHECK (reviewer_role IN ('functional', 'security', 'ui-e2e')),
  target_sha text NOT NULL CHECK (target_sha ~ '^[a-f0-9]{40,64}$'),
  evidence_sha256 char(64) NOT NULL CHECK (evidence_sha256 ~ '^[a-f0-9]{64}$'),
  verdict_digest_sha256 char(64) NOT NULL CHECK (verdict_digest_sha256 ~ '^[a-f0-9]{64}$'),
  passed boolean NOT NULL,
  findings_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  FOREIGN KEY (tenant_id, source_artifact_id) REFERENCES agent.context_artifacts(tenant_id, id)
);

CREATE INDEX dev_review_artifacts_lookup_idx
  ON agent.dev_review_artifacts (tenant_id, project_id, cycle_id, target_sha, reviewer_role);

CREATE TABLE agent.dev_debug_findings (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  cycle_id text NOT NULL,
  preview_lease_id uuid,
  commit_sha text NOT NULL CHECK (commit_sha ~ '^[a-f0-9]{40,64}$'),
  source_url_redacted text NOT NULL,
  console_errors_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  network_errors_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  layout_issues_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  repro_test_id text,
  repro_before_fail_digest char(64) CHECK (repro_before_fail_digest IS NULL OR repro_before_fail_digest ~ '^[a-f0-9]{64}$'),
  repro_after_pass_digest char(64) CHECK (repro_after_pass_digest IS NULL OR repro_after_pass_digest ~ '^[a-f0-9]{64}$'),
  trace_artifact_id uuid,
  status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'FIXED', 'VERIFIED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id)
);

CREATE INDEX dev_debug_findings_cycle_idx ON agent.dev_debug_findings (tenant_id, cycle_id, created_at DESC);

-- Optional path + body digest binding on Sentinel egress permits (derived server-side for dev_github_pr).
ALTER TABLE agent.egress_permits
  ADD COLUMN IF NOT EXISTS target_path text,
  ADD COLUMN IF NOT EXISTS body_sha256 char(64) CHECK (body_sha256 IS NULL OR body_sha256 ~ '^[0-9a-f]{64}$');

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'dev_projects',
    'dev_demo_artifacts',
    'dev_design_decisions',
    'dev_pr_records',
    'dev_preview_instances',
    'dev_review_artifacts',
    'dev_debug_findings'
  ] LOOP
    EXECUTE format('ALTER TABLE agent.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON TABLE agent.%I FROM PUBLIC', t);
    EXECUTE format($p$CREATE POLICY api_tenant ON agent.%I TO aiagent_api
      USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)$p$, t);
    EXECUTE format($p$CREATE POLICY runtime_all ON agent.%I TO aiagent_runtime, aiagent_tool_gateway, aiagent_ops
      USING (true) WITH CHECK (true)$p$, t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON
  agent.dev_projects,
  agent.dev_demo_artifacts,
  agent.dev_design_decisions,
  agent.dev_pr_records,
  agent.dev_preview_instances,
  agent.dev_review_artifacts,
  agent.dev_debug_findings
  TO aiagent_api, aiagent_runtime, aiagent_tool_gateway;
GRANT SELECT ON
  agent.dev_projects,
  agent.dev_demo_artifacts,
  agent.dev_design_decisions,
  agent.dev_pr_records,
  agent.dev_preview_instances,
  agent.dev_review_artifacts,
  agent.dev_debug_findings
  TO aiagent_ops;
