-- 0001_core: canonical control-plane tables (CHAT_10 10-01 §5.2 physical v1, STEP 3 subset).
-- Applied by aiagent_core.migrate in ONE transaction together with its schema_migrations row (checksum bound by the
-- runner). Out of scope for this migration (follow-up migrations, owners not yet agreed): conversations/threads/
-- messages, artifacts/evidence, browser sessions, watches/observations/notifications, memory.
CREATE SCHEMA agent;

CREATE TABLE agent.schema_control (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version integer NOT NULL CHECK (schema_version >= 0),
  min_compatible_reader integer NOT NULL CHECK (min_compatible_reader >= 0),
  min_compatible_writer integer NOT NULL CHECK (min_compatible_writer >= 0),
  -- Restore generation (backup-upgrade-and-recovery): bumped by the operator after PITR/failover. Claims, queue
  -- pointers and gateway admissions carry it; anything minted under an older incarnation is refused.
  restore_incarnation bigint NOT NULL DEFAULT 1 CHECK (restore_incarnation BETWEEN 1 AND 9223372036854775807),
  dispatch_paused boolean NOT NULL DEFAULT false,
  changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO agent.schema_control(singleton, schema_version, min_compatible_reader, min_compatible_writer)
VALUES (true, 0, 0, 0);

-- ---------------------------------------------------------------- identity / scope (CHAT_09 owns policy)
CREATE TABLE agent.tenants (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz
);
CREATE TABLE agent.users (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz
);
CREATE TABLE agent.tenant_memberships (
  tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
  user_id uuid NOT NULL REFERENCES agent.users(id),
  role_code text NOT NULL DEFAULT 'member' CHECK (role_code IN ('member', 'admin')),
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz,
  PRIMARY KEY (tenant_id, user_id)
);
-- Workspace = physical scope inside a tenant (R2-05). Owned by one member.
CREATE TABLE agent.workspaces (
  tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, id, owner_user_id),
  FOREIGN KEY (tenant_id, owner_user_id) REFERENCES agent.tenant_memberships(tenant_id, user_id)
);
-- API bearer credentials: only the SHA-256 of the token is stored.
CREATE TABLE agent.api_credentials (
  token_sha256 char(64) PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz,
  FOREIGN KEY (tenant_id, workspace_id, user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id)
);

-- ---------------------------------------------------------------- Task / Goal / Run (CHAT_01/02)
CREATE TABLE agent.tasks (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  parent_task_id uuid,
  retry_of_task_id uuid,
  created_from_schedule_id uuid,
  trigger_key text,
  profile_code text NOT NULL DEFAULT 'default' CHECK (profile_code ~ '^[a-z][a-z0-9_-]{0,63}$'),
  goal_revision bigint NOT NULL DEFAULT 1 CHECK (goal_revision BETWEEN 1 AND 9223372036854775807),
  active_run_id uuid,
  status_code text NOT NULL CHECK (status_code IN ('QUEUED','RUNNING','WAITING_INPUT','PAUSED','WAITING_RECONCILIATION',
    'CANCELLING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')),
  canonical_schema_version integer NOT NULL DEFAULT 1 CHECK (canonical_schema_version > 0),
  deadline_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id),
  FOREIGN KEY (tenant_id, parent_task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, retry_of_task_id) REFERENCES agent.tasks(tenant_id, id),
  CHECK (retry_of_task_id IS NULL OR retry_of_task_id <> id),
  CHECK (parent_task_id IS NULL OR parent_task_id <> id)
);
CREATE INDEX tasks_by_owner ON agent.tasks (tenant_id, owner_user_id, created_at DESC);
CREATE INDEX tasks_by_status ON agent.tasks (tenant_id, status_code, updated_at DESC);
CREATE UNIQUE INDEX task_trigger_once ON agent.tasks (tenant_id, created_from_schedule_id, trigger_key)
  WHERE created_from_schedule_id IS NOT NULL AND trigger_key IS NOT NULL;

CREATE TABLE agent.task_goal_revisions (
  tenant_id uuid NOT NULL,
  task_id uuid NOT NULL,
  revision bigint NOT NULL CHECK (revision BETWEEN 1 AND 9223372036854775807),
  goal_text text NOT NULL CHECK (length(goal_text) BETWEEN 1 AND 20000),
  goal_sha256 char(64) NOT NULL CHECK (goal_sha256 ~ '^[0-9a-f]{64}$'),
  created_by_user_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, task_id, revision),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);
ALTER TABLE agent.tasks ADD CONSTRAINT tasks_current_goal_fk FOREIGN KEY (tenant_id, id, goal_revision)
  REFERENCES agent.task_goal_revisions(tenant_id, task_id, revision) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE agent.runs (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no BETWEEN 1 AND 1000000),
  input_goal_revision bigint NOT NULL CHECK (input_goal_revision BETWEEN 1 AND 9223372036854775807),
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch BETWEEN 0 AND 9223372036854775807),
  lease_owner_ref text,
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  status_code text NOT NULL CHECK (status_code IN ('CREATED','CLAIMED','RUNNING','WAITING_APPROVAL','WAITING_INPUT',
    'PAUSED','WAITING_RECONCILIATION','CANCELLING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED',
    'BUDGET_EXHAUSTED')),
  cancel_requested boolean NOT NULL DEFAULT false,
  pause_requested boolean NOT NULL DEFAULT false,
  state_schema_version integer NOT NULL DEFAULT 1 CHECK (state_schema_version > 0),
  -- Checkpoint HEAD: revision counter + pointer to the newest immutable entry (CAS under row lock).
  head_revision bigint NOT NULL DEFAULT 0 CHECK (head_revision BETWEEN 0 AND 9223372036854775807),
  current_checkpoint_id uuid,
  -- Derived cache pointer (LangGraph thread id). Never authoritative; discarded on mismatch.
  framework_ref text CHECK (framework_ref IS NULL OR length(framework_ref) <= 200),
  restore_incarnation bigint NOT NULL DEFAULT 1 CHECK (restore_incarnation >= 1),
  -- Model policy / budget (Model Router 02-09, Budget 02-10; PROVISIONAL physical columns).
  allowed_models text[] NOT NULL DEFAULT '{}',
  token_budget bigint NOT NULL DEFAULT 200000 CHECK (token_budget >= 0),
  tokens_reserved bigint NOT NULL DEFAULT 0 CHECK (tokens_reserved >= 0),
  tokens_used bigint NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  max_model_calls integer NOT NULL DEFAULT 64 CHECK (max_model_calls > 0),
  model_calls integer NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
  result_text text,
  error_code text,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, task_id, id),
  UNIQUE (tenant_id, task_id, attempt_no),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, input_goal_revision)
    REFERENCES agent.task_goal_revisions(tenant_id, task_id, revision)
);
ALTER TABLE agent.tasks ADD CONSTRAINT task_active_run_fk FOREIGN KEY (tenant_id, id, active_run_id)
  REFERENCES agent.runs(tenant_id, task_id, id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX runs_by_task ON agent.runs (tenant_id, task_id, attempt_no DESC);
CREATE INDEX runs_expired_lease ON agent.runs (lease_expires_at)
  WHERE status_code IN ('CLAIMED', 'RUNNING');

-- Append-only Checkpoint HEAD entries (canonical transcript). One row per immutable entry.
CREATE TABLE agent.run_checkpoints (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  checkpoint_revision bigint NOT NULL CHECK (checkpoint_revision BETWEEN 1 AND 9223372036854775807),
  stream text NOT NULL DEFAULT 'main' CHECK (stream ~ '^(main|summarize|subagent:[a-z0-9_-]{1,64})$'),
  entry_kind text NOT NULL CHECK (entry_kind IN ('user', 'assistant', 'tool', 'summary', 'system')),
  request_key text CHECK (request_key IS NULL OR request_key ~ '^[0-9a-f]{64}$|^seed-[0-9]+$'),
  tool_call_id text CHECK (tool_call_id IS NULL OR tool_call_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  writer_ref text NOT NULL,
  writer_lease_epoch bigint NOT NULL CHECK (writer_lease_epoch BETWEEN 0 AND 9223372036854775807),
  applied_goal_revision bigint NOT NULL CHECK (applied_goal_revision BETWEEN 1 AND 9223372036854775807),
  checkpoint_schema_version integer NOT NULL DEFAULT 1 CHECK (checkpoint_schema_version > 0),
  checkpoint_json jsonb NOT NULL CHECK (jsonb_typeof(checkpoint_json) = 'object'),
  snapshot_sha256 char(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, run_id, checkpoint_revision),
  UNIQUE (tenant_id, id, run_id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, task_id, applied_goal_revision)
    REFERENCES agent.task_goal_revisions(tenant_id, task_id, revision)
);
CREATE UNIQUE INDEX checkpoint_assistant_once ON agent.run_checkpoints (tenant_id, run_id, stream, request_key)
  WHERE entry_kind = 'assistant';
CREATE UNIQUE INDEX checkpoint_tool_once ON agent.run_checkpoints (tenant_id, run_id, tool_call_id)
  WHERE entry_kind = 'tool';
ALTER TABLE agent.runs ADD CONSTRAINT runs_checkpoint_head_fk
  FOREIGN KEY (tenant_id, current_checkpoint_id, id) REFERENCES agent.run_checkpoints(tenant_id, id, run_id)
  DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------- Domain events (CHAT_01 01-02)
CREATE TABLE agent.event_aggregate_counters (
  tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
  aggregate_type text NOT NULL CHECK (aggregate_type IN ('task','run','watch','approval','tool','evidence','notification')),
  aggregate_id uuid NOT NULL,
  next_aggregate_sequence bigint NOT NULL DEFAULT 1 CHECK (next_aggregate_sequence BETWEEN 1 AND 9223372036854775807),
  PRIMARY KEY (tenant_id, aggregate_type, aggregate_id)
);
CREATE TABLE agent.tenant_event_counters (
  tenant_id uuid PRIMARY KEY REFERENCES agent.tenants(id),
  next_delivery_offset bigint NOT NULL DEFAULT 1 CHECK (next_delivery_offset BETWEEN 1 AND 9223372036854775807)
);
CREATE TABLE agent.domain_events (
  tenant_id uuid NOT NULL REFERENCES agent.tenants(id),
  id uuid NOT NULL,
  aggregate_type text NOT NULL CHECK (aggregate_type IN ('task','run','watch','approval','tool','evidence','notification')),
  aggregate_id uuid NOT NULL,
  aggregate_sequence bigint NOT NULL CHECK (aggregate_sequence > 0),
  delivery_offset bigint NOT NULL CHECK (delivery_offset > 0),
  event_kind text NOT NULL CHECK (event_kind ~ '^[a-z]+\.[a-z_]+$'),
  schema_version text NOT NULL DEFAULT '1.0' CHECK (schema_version ~ '^[1-9][0-9]*[.][0-9]+$'),
  payload_schema_version text NOT NULL DEFAULT '1.0' CHECK (payload_schema_version ~ '^[1-9][0-9]*[.][0-9]+$'),
  payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
  actor_json jsonb NOT NULL CHECK (jsonb_typeof(actor_json) = 'object'),
  audience_code text NOT NULL CHECK (audience_code IN ('owner_private', 'operator_audit')),
  owner_user_id uuid,
  task_id uuid,
  run_id uuid,
  causation_event_id uuid,
  correlation_id text NOT NULL CHECK (length(correlation_id) BETWEEN 1 AND 256),
  trace_id text,
  occurred_at timestamptz NOT NULL,
  committed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, aggregate_type, aggregate_id, aggregate_sequence),
  UNIQUE (tenant_id, delivery_offset),
  FOREIGN KEY (tenant_id, aggregate_type, aggregate_id)
    REFERENCES agent.event_aggregate_counters(tenant_id, aggregate_type, aggregate_id),
  FOREIGN KEY (tenant_id, owner_user_id) REFERENCES agent.tenant_memberships(tenant_id, user_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, causation_event_id) REFERENCES agent.domain_events(tenant_id, id) DEFERRABLE INITIALLY DEFERRED,
  CHECK (run_id IS NULL OR task_id IS NOT NULL),
  CHECK (audience_code <> 'owner_private' OR owner_user_id IS NOT NULL)
);
CREATE INDEX domain_events_task_replay ON agent.domain_events (tenant_id, task_id, delivery_offset) WHERE task_id IS NOT NULL;
CREATE INDEX domain_events_aggregate ON agent.domain_events (tenant_id, aggregate_type, aggregate_id, aggregate_sequence DESC);

-- ---------------------------------------------------------------- Tool intent / approval / ledger (CHAT_03)
CREATE TABLE agent.tool_intents (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  tool_key text NOT NULL CHECK (tool_key ~ '^[a-z][a-z0-9_.]{0,63}$'),
  invocation_key text NOT NULL CHECK (length(invocation_key) BETWEEN 1 AND 128),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  request_schema_version integer NOT NULL DEFAULT 1 CHECK (request_schema_version > 0),
  safe_arguments_json jsonb NOT NULL CHECK (jsonb_typeof(safe_arguments_json) = 'object'),
  risk_class text NOT NULL CHECK (risk_class IN ('read', 'write_reversible', 'external_irreversible')),
  goal_revision bigint NOT NULL CHECK (goal_revision BETWEEN 1 AND 9223372036854775807),
  status_code text NOT NULL CHECK (status_code IN ('DONE','PENDING_APPROVAL','SENDING','CONFIRMED','DENIED','EXPIRED',
    'STALE_GOAL','REVOKED','CANCELLED','UNKNOWN','BUDGET_EXHAUSTED','FAILED')),
  stream text NOT NULL DEFAULT 'main',
  result_json jsonb,
  submissions integer NOT NULL DEFAULT 1 CHECK (submissions >= 1),
  policy_decision_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, run_id, invocation_key),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, task_id, goal_revision) REFERENCES agent.task_goal_revisions(tenant_id, task_id, revision)
);
CREATE INDEX tool_intents_by_run ON agent.tool_intents (tenant_id, run_id, created_at DESC);
CREATE INDEX tool_intents_unknown ON agent.tool_intents (tenant_id, status_code) WHERE status_code IN ('UNKNOWN', 'SENDING');

CREATE TABLE agent.approval_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  tool_key text NOT NULL,
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  goal_revision bigint NOT NULL CHECK (goal_revision BETWEEN 1 AND 9223372036854775807),
  safe_summary text NOT NULL CHECK (length(safe_summary) <= 2000),
  decision_code text NOT NULL DEFAULT 'PENDING' CHECK (decision_code IN ('PENDING','APPROVED','REJECTED','REVOKED','EXPIRED')),
  decided_by_json jsonb,
  expires_at timestamptz NOT NULL,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  requested_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, tool_intent_id),
  FOREIGN KEY (tenant_id, tool_intent_id) REFERENCES agent.tool_intents(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  FOREIGN KEY (tenant_id, owner_user_id) REFERENCES agent.tenant_memberships(tenant_id, user_id)
);
CREATE INDEX approvals_pending ON agent.approval_records (tenant_id, run_id) WHERE decision_code = 'PENDING';

CREATE TABLE agent.tool_attempts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  fence_token bigint NOT NULL CHECK (fence_token BETWEEN 1 AND 9223372036854775807),
  worker_ref text NOT NULL,
  run_lease_epoch bigint NOT NULL CHECK (run_lease_epoch >= 0),
  status_code text NOT NULL CHECK (status_code IN ('SENDING', 'CONFIRMED', 'FAILED', 'UNKNOWN')),
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, tool_intent_id, attempt_no),
  UNIQUE (tenant_id, tool_intent_id, fence_token),
  FOREIGN KEY (tenant_id, tool_intent_id) REFERENCES agent.tool_intents(tenant_id, id)
);

CREATE TABLE agent.side_effect_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  effect_key text NOT NULL CHECK (length(effect_key) BETWEEN 1 AND 200),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  outcome_code text NOT NULL CHECK (outcome_code IN ('SENDING','CONFIRMED','FAILED','UNKNOWN','RECONCILED_SENT',
    'RECONCILED_NOT_SENT')),
  attempt_fence bigint NOT NULL CHECK (attempt_fence BETWEEN 1 AND 9223372036854775807),
  external_receipt_ref text,
  egress_permit_ref text,
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, effect_key),
  UNIQUE (tenant_id, tool_intent_id),
  FOREIGN KEY (tenant_id, tool_intent_id) REFERENCES agent.tool_intents(tenant_id, id)
);

-- ---------------------------------------------------------------- Model attempts / usage (02-09 / 10-04)
CREATE TABLE agent.model_attempts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  stream text NOT NULL,
  request_identity char(64) NOT NULL CHECK (request_identity ~ '^[0-9a-f]{64}$'),
  model_key text NOT NULL,
  provider_key text,
  run_lease_epoch bigint NOT NULL CHECK (run_lease_epoch >= 0),
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  admitted boolean NOT NULL,
  cached boolean NOT NULL DEFAULT false,
  reason_code text,
  status_code text NOT NULL CHECK (status_code IN ('DENIED','RESERVED','STREAMING','COMPLETED','FAILED','CANCELLED','REPLAYED')),
  tool_surface text[] NOT NULL DEFAULT '{}',
  tokens_reserved bigint NOT NULL DEFAULT 0 CHECK (tokens_reserved >= 0),
  input_tokens bigint CHECK (input_tokens >= 0),
  output_tokens bigint CHECK (output_tokens >= 0),
  usage_quality text NOT NULL DEFAULT 'UNKNOWN' CHECK (usage_quality IN ('MEASURED', 'ESTIMATED', 'UNKNOWN')),
  provider_attempts integer NOT NULL DEFAULT 0 CHECK (provider_attempts >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id)
);
CREATE INDEX model_attempts_by_run ON agent.model_attempts (tenant_id, run_id, created_at);

-- Provisional streamed answer text (public text only, after the Stream Privacy Gate). Never the committed answer.
CREATE TABLE agent.run_stream_chunks (
  tenant_id uuid NOT NULL,
  model_attempt_id uuid NOT NULL,
  run_id uuid NOT NULL,
  seq integer NOT NULL CHECK (seq >= 0),
  text_delta text NOT NULL CHECK (length(text_delta) <= 65536),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, model_attempt_id, seq),
  FOREIGN KEY (tenant_id, model_attempt_id) REFERENCES agent.model_attempts(tenant_id, id)
);

CREATE TABLE agent.cost_usage_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid,
  usage_key text NOT NULL,
  metering_kind_code text NOT NULL,
  usage_kind text NOT NULL CHECK (usage_kind IN ('MEASURED', 'ESTIMATED', 'UNKNOWN')),
  provider_key text,
  logical_call_id text,
  input_units bigint CHECK (input_units >= 0),
  output_units bigint CHECK (output_units >= 0),
  estimated_cost_micro_usd bigint CHECK (estimated_cost_micro_usd >= 0),
  measured_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, usage_key),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id)
);

-- ---------------------------------------------------------------- Outbox (10-02)
CREATE TABLE agent.outbox_messages (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  topic text NOT NULL CHECK (topic IN ('run', 'notification', 'sse')),
  dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
  event_id uuid NOT NULL,
  payload_schema_version integer NOT NULL DEFAULT 1 CHECK (payload_schema_version > 0),
  safe_payload_json jsonb NOT NULL CHECK (jsonb_typeof(safe_payload_json) = 'object'),
  payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  restore_incarnation bigint NOT NULL DEFAULT 1 CHECK (restore_incarnation >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  dispatch_attempts integer NOT NULL DEFAULT 0 CHECK (dispatch_attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  relay_lease_epoch bigint NOT NULL DEFAULT 0 CHECK (relay_lease_epoch BETWEEN 0 AND 9223372036854775807),
  relay_owner_ref text,
  relay_lease_until timestamptz,
  last_error_code text,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, topic, dedupe_key),
  FOREIGN KEY (tenant_id, event_id) REFERENCES agent.domain_events(tenant_id, id)
);
CREATE INDEX outbox_ready ON agent.outbox_messages (next_attempt_at, id) WHERE dispatched_at IS NULL;

-- ---------------------------------------------------------------- API idempotency / audit
CREATE TABLE agent.api_idempotency (
  tenant_id uuid NOT NULL,
  actor_user_id uuid NOT NULL,
  operation_name text NOT NULL,
  operation_key text NOT NULL CHECK (length(operation_key) BETWEEN 8 AND 200),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  response_http_status integer,
  response_json jsonb,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, actor_user_id, operation_name, operation_key),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES agent.tenant_memberships(tenant_id, user_id)
);
CREATE INDEX api_idempotency_expiry ON agent.api_idempotency (expires_at);

CREATE TABLE agent.audit_records (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  subject_ref text,
  action_code text NOT NULL,
  target_type text NOT NULL,
  target_ref text,
  redacted_detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX audit_recent ON agent.audit_records (tenant_id, recorded_at DESC);

-- ---------------------------------------------------------------- immutability guards
-- Checkpoint HEAD entries and domain events are append-only. DELETE is allowed only inside the retention GC
-- function (session flag set by agent.gc_terminal_runs, which only the ops role may execute).
CREATE FUNCTION agent.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('aiagent.retention_gc', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'aiagent: % on %.% is forbidden (append-only)', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER run_checkpoints_append_only BEFORE UPDATE OR DELETE ON agent.run_checkpoints
  FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();
CREATE TRIGGER domain_events_append_only BEFORE UPDATE OR DELETE ON agent.domain_events
  FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();
CREATE TRIGGER goal_revisions_append_only BEFORE UPDATE OR DELETE ON agent.task_goal_revisions
  FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();

-- Terminal runs never change state again (01-03: terminal -> no transition). Enforced in the DB as well as in SQL.
CREATE FUNCTION agent.guard_terminal_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
     AND NEW.status_code IS DISTINCT FROM OLD.status_code THEN
    RAISE EXCEPTION 'aiagent: terminal run % cannot change state', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.lease_epoch < OLD.lease_epoch OR NEW.head_revision < OLD.head_revision THEN
    RAISE EXCEPTION 'aiagent: run fences are monotonic' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER runs_terminal_guard BEFORE UPDATE ON agent.runs
  FOR EACH ROW EXECUTE FUNCTION agent.guard_terminal_run();
CREATE FUNCTION agent.guard_terminal_task() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
     AND NEW.status_code IS DISTINCT FROM OLD.status_code THEN
    RAISE EXCEPTION 'aiagent: terminal task % cannot change state', OLD.id USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.goal_revision < OLD.goal_revision THEN
    RAISE EXCEPTION 'aiagent: goal revision is monotonic' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tasks_terminal_guard BEFORE UPDATE ON agent.tasks
  FOR EACH ROW EXECUTE FUNCTION agent.guard_terminal_task();
