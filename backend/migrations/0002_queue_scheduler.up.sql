-- 0002_queue_scheduler: ADR-039 QueuePort (self-built PostgreSQL queue) and the Scheduler (07 scheduler spec).
-- The queue is transport only: payloads are opaque pointers (RunJobPointerV1); authority stays in agent.*.
CREATE SCHEMA queue;

CREATE TABLE queue.queues (
  name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_.]{0,63}$'),
  paused boolean NOT NULL DEFAULT false,
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 100),
  base_backoff_ms integer NOT NULL DEFAULT 2000 CHECK (base_backoff_ms > 0),
  max_backoff_ms integer NOT NULL DEFAULT 300000 CHECK (max_backoff_ms > 0),
  visibility_ms integer NOT NULL DEFAULT 30000 CHECK (visibility_ms >= 1000),
  per_tenant_active_limit integer NOT NULL DEFAULT 2 CHECK (per_tenant_active_limit > 0),
  global_active_limit integer NOT NULL DEFAULT 8 CHECK (global_active_limit > 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- Queue names are a hardcoded allowlist: arbitrary queue names from payloads/HTML are rejected by the FK.
INSERT INTO queue.queues(name) VALUES ('run');

CREATE TABLE queue.jobs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue_name text NOT NULL REFERENCES queue.queues(name),
  tenant_id uuid NOT NULL,
  dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND pg_column_size(payload) <= 8192),
  payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'READY' CHECK (status IN ('READY', 'CLAIMED', 'DONE', 'DEAD', 'CANCELLED')),
  priority smallint NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
  -- Attempt fence: incremented on every claim; only the current attempt may heartbeat / ack / nack.
  attempt_fence bigint NOT NULL DEFAULT 0 CHECK (attempt_fence BETWEEN 0 AND 9223372036854775807),
  locked_by text,
  locked_until timestamptz,
  last_error_code text CHECK (last_error_code IS NULL OR last_error_code ~ '^[A-Z0-9_]{1,64}$'),
  outcome_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (queue_name, dedupe_key),
  CHECK ((status = 'CLAIMED') = (locked_until IS NOT NULL))
);
CREATE INDEX jobs_ready ON queue.jobs (queue_name, priority DESC, run_after, id) WHERE status = 'READY';
CREATE INDEX jobs_claimed ON queue.jobs (queue_name, locked_until) WHERE status = 'CLAIMED';
CREATE INDEX jobs_tenant_active ON queue.jobs (queue_name, tenant_id) WHERE status = 'CLAIMED';

CREATE TABLE queue.job_attempts (
  job_id bigint NOT NULL REFERENCES queue.jobs(id),
  attempt_fence bigint NOT NULL CHECK (attempt_fence >= 1),
  worker_ref text NOT NULL,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  outcome_code text,
  PRIMARY KEY (job_id, attempt_fence)
);

CREATE TABLE queue.dead_letters (
  job_id bigint PRIMARY KEY REFERENCES queue.jobs(id),
  queue_name text NOT NULL,
  tenant_id uuid NOT NULL,
  reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]{1,64}$'),
  attempts integer NOT NULL,
  moved_at timestamptz NOT NULL DEFAULT now(),
  replayed_at timestamptz,
  replay_decision text
);

-- ---------------------------------------------------------------- Scheduler (CHAT_07). Schedule state is authority;
-- the occurrence row is the dedupe point (one Task per schedule slot, JQ-T07).
CREATE TABLE agent.schedules (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('one_shot', 'cron')),
  cron_expr text CHECK (cron_expr IS NULL OR length(cron_expr) BETWEEN 9 AND 120),
  timezone_id text NOT NULL CHECK (length(timezone_id) BETWEEN 1 AND 64),
  run_at timestamptz,
  goal_text text NOT NULL CHECK (length(goal_text) BETWEEN 1 AND 20000),
  profile_code text NOT NULL DEFAULT 'default',
  catchup_policy text NOT NULL DEFAULT 'latest' CHECK (catchup_policy IN ('skip', 'latest', 'all_bounded')),
  max_catchup integer NOT NULL DEFAULT 3 CHECK (max_catchup BETWEEN 1 AND 100),
  enabled boolean NOT NULL DEFAULT true,
  definition_revision bigint NOT NULL DEFAULT 1 CHECK (definition_revision BETWEEN 1 AND 9223372036854775807),
  next_fire_at timestamptz,
  last_slot_at timestamptz,
  record_version bigint NOT NULL DEFAULT 1 CHECK (record_version BETWEEN 1 AND 9223372036854775807),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id),
  CHECK ((kind = 'cron') = (cron_expr IS NOT NULL)),
  CHECK ((kind = 'one_shot') = (run_at IS NOT NULL))
);
CREATE INDEX schedules_due ON agent.schedules (next_fire_at) WHERE enabled AND next_fire_at IS NOT NULL;
ALTER TABLE agent.tasks ADD CONSTRAINT task_schedule_fk FOREIGN KEY (tenant_id, created_from_schedule_id)
  REFERENCES agent.schedules(tenant_id, id);

CREATE TABLE agent.schedule_occurrences (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  schedule_id uuid NOT NULL,
  definition_revision bigint NOT NULL,
  slot_at timestamptz NOT NULL,
  task_id uuid NOT NULL,
  fired_at timestamptz NOT NULL DEFAULT now(),
  late_by_ms bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, schedule_id, slot_at),
  FOREIGN KEY (tenant_id, schedule_id) REFERENCES agent.schedules(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);
