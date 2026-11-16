-- 0008_dev_and_automation: PostgreSQL persistence for Development cycles/previews and Automation campaigns/watches.

CREATE TABLE agent.dev_cycles (
  tenant_id uuid NOT NULL,
  id text NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  workspace_id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  version integer NOT NULL CHECK (version >= 0),
  phase text NOT NULL,
  cycle_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent.runs(tenant_id, id)
);

CREATE INDEX dev_cycles_scope ON agent.dev_cycles (tenant_id, owner_user_id, project_id);

CREATE TABLE agent.dev_diff_claims (
  tenant_id uuid NOT NULL,
  project_id text NOT NULL,
  diff_sha256 char(64) NOT NULL,
  cycle_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, project_id, diff_sha256)
);

CREATE TABLE agent.preview_leases (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id text NOT NULL,
  workspace_id uuid NOT NULL,
  commit_sha text NOT NULL,
  target_port integer NOT NULL CHECK (target_port BETWEEN 1024 AND 65535),
  token_sha256 char(64) NOT NULL,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REVOKED')),
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  PRIMARY KEY (tenant_id, id)
);

CREATE INDEX preview_leases_scope ON agent.preview_leases (tenant_id, owner_user_id, project_id) WHERE status = 'ACTIVE';

CREATE TABLE agent.automation_campaigns (
  tenant_id uuid NOT NULL,
  id text NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  state text NOT NULL,
  control_version integer NOT NULL CHECK (control_version >= 1),
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  campaign_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES agent.workspaces(tenant_id, id)
);

CREATE TABLE agent.automation_watches (
  tenant_id uuid NOT NULL,
  watch_id text NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  revision bigint NOT NULL CHECK (revision >= 1),
  max_occurrences integer NOT NULL CHECK (max_occurrences >= 1),
  timezone_id text NOT NULL,
  start_utc timestamptz NOT NULL,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, watch_id),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES agent.workspaces(tenant_id, id)
);

CREATE TABLE agent.automation_watch_slots (
  tenant_id uuid NOT NULL,
  watch_id text NOT NULL,
  slot_iso text NOT NULL,
  dedupe_key text NOT NULL,
  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENT')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, watch_id, slot_iso),
  UNIQUE (tenant_id, dedupe_key),
  FOREIGN KEY (tenant_id, watch_id) REFERENCES agent.automation_watches(tenant_id, watch_id)
);

ALTER TABLE agent.dev_cycles ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.dev_diff_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.preview_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.automation_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.automation_watches ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.automation_watch_slots ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON agent.dev_cycles, agent.dev_diff_claims, agent.preview_leases, agent.automation_campaigns,
  agent.automation_watches, agent.automation_watch_slots FROM PUBLIC;

CREATE POLICY dev_cycles_api_scope ON agent.dev_cycles TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
CREATE POLICY dev_diff_claims_api_scope ON agent.dev_diff_claims TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
CREATE POLICY preview_leases_api_scope ON agent.preview_leases TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
CREATE POLICY automation_campaigns_api_scope ON agent.automation_campaigns TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
CREATE POLICY automation_watches_api_scope ON agent.automation_watches TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
CREATE POLICY automation_watch_slots_api_scope ON agent.automation_watch_slots TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

GRANT SELECT, INSERT, UPDATE ON agent.dev_cycles, agent.dev_diff_claims, agent.preview_leases,
  agent.automation_campaigns, agent.automation_watches, agent.automation_watch_slots
  TO aiagent_api, aiagent_ops;
GRANT SELECT, INSERT, UPDATE ON agent.dev_cycles, agent.dev_diff_claims, agent.preview_leases
  TO aiagent_tool_gateway, aiagent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent.automation_campaigns, agent.automation_watches, agent.automation_watch_slots
  TO aiagent_relay;
