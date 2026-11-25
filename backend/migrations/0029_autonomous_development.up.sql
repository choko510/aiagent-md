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
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES agent.workspaces(tenant_id, id)
);

CREATE INDEX dev_projects_owner_idx ON agent.dev_projects (tenant_id, owner_user_id, created_at DESC);

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
  demo_snapshot_digest char(64) CHECK (demo_snapshot_digest IS NULL OR demo_snapshot_digest ~ '^[a-f0-9]{64}$'),
  feedback text,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, cycle_id, design_revision),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id)
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
  effect_status text NOT NULL DEFAULT 'PENDING' CHECK (effect_status IN ('PENDING', 'CONFIRMED', 'EFFECT_UNKNOWN', 'FAILED')),
  ci_state text NOT NULL DEFAULT 'UNKNOWN' CHECK (ci_state IN ('UNKNOWN', 'PENDING', 'PASS', 'FAIL', 'CONFLICT', 'ESCALATED_LIMIT_REACHED')),
  repair_attempts integer NOT NULL DEFAULT 0 CHECK (repair_attempts >= 0),
  max_repair_attempts integer NOT NULL DEFAULT 3 CHECK (max_repair_attempts >= 1),
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES agent.dev_projects(tenant_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id)
);

CREATE INDEX dev_pr_records_project_idx ON agent.dev_pr_records (tenant_id, project_id, created_at DESC);

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

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['dev_projects','dev_design_decisions','dev_pr_records','dev_debug_findings'] LOOP
    EXECUTE format('ALTER TABLE agent.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON TABLE agent.%I FROM PUBLIC', t);
    EXECUTE format($p$CREATE POLICY api_tenant ON agent.%I TO aiagent_api
      USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)$p$, t);
    EXECUTE format($p$CREATE POLICY runtime_all ON agent.%I TO aiagent_runtime, aiagent_tool_gateway, aiagent_ops
      USING (true) WITH CHECK (true)$p$, t);
  END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON agent.dev_projects, agent.dev_design_decisions, agent.dev_pr_records, agent.dev_debug_findings
  TO aiagent_api, aiagent_runtime, aiagent_tool_gateway;
GRANT SELECT ON agent.dev_projects, agent.dev_design_decisions, agent.dev_pr_records, agent.dev_debug_findings
  TO aiagent_ops;
