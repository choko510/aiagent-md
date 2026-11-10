-- 0005_roles_grants_rls: least-privilege group roles per process and tenant Row Level Security for the API role.
-- Group roles are NOLOGIN; deploy bootstrap creates LOGIN users and grants exactly one group to each
-- (deploy/postgres/bootstrap-logins.sql). No role except the migration owner has DDL anywhere.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['aiagent_api','aiagent_runtime','aiagent_checkpointer','aiagent_model_gateway',
                           'aiagent_tool_gateway','aiagent_relay','aiagent_sentinel','aiagent_ops'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOBYPASSRLS', r);
    END IF;
  END LOOP;
END $$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA agent, queue, lg_checkpoint FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agent, lg_checkpoint FROM PUBLIC;

GRANT USAGE ON SCHEMA agent TO aiagent_api, aiagent_runtime, aiagent_model_gateway, aiagent_tool_gateway,
  aiagent_relay, aiagent_sentinel, aiagent_ops;
GRANT USAGE ON SCHEMA queue TO aiagent_runtime, aiagent_relay, aiagent_ops;
GRANT USAGE ON SCHEMA lg_checkpoint TO aiagent_checkpointer, aiagent_ops;

-- Shared reads needed by every control-plane service.
GRANT SELECT ON agent.schema_control, agent.tenants, agent.users, agent.tenant_memberships, agent.workspaces
  TO aiagent_api, aiagent_runtime, aiagent_model_gateway, aiagent_tool_gateway, aiagent_relay, aiagent_sentinel, aiagent_ops;

-- Event + Outbox writers (domain mutation, event and outbox in ONE transaction).
GRANT SELECT, INSERT, UPDATE ON agent.event_aggregate_counters, agent.tenant_event_counters
  TO aiagent_api, aiagent_runtime, aiagent_tool_gateway, aiagent_relay;
GRANT SELECT, INSERT ON agent.domain_events TO aiagent_api, aiagent_runtime, aiagent_tool_gateway, aiagent_relay;
GRANT SELECT, INSERT ON agent.outbox_messages TO aiagent_api, aiagent_runtime, aiagent_tool_gateway, aiagent_relay;

-- API: user commands. Tenant isolation by RLS (below) + owner checks in SQL.
GRANT SELECT, INSERT ON agent.tasks, agent.runs TO aiagent_api;
GRANT SELECT, INSERT, UPDATE ON agent.api_idempotency, agent.schedules TO aiagent_api;
-- Column grants: user commands change state/goal/control flags only, never budget, model policy or fences.
GRANT UPDATE (status_code, goal_revision, active_run_id, record_version, updated_at) ON agent.tasks TO aiagent_api;
GRANT UPDATE (status_code, cancel_requested, pause_requested, ended_at, head_revision, current_checkpoint_id,
  record_version, updated_at) ON agent.runs TO aiagent_api;
GRANT SELECT, INSERT ON agent.task_goal_revisions, agent.run_checkpoints, agent.audit_records TO aiagent_api;
GRANT SELECT, UPDATE ON agent.approval_records TO aiagent_api;
GRANT SELECT ON agent.tool_intents, agent.side_effect_records, agent.model_attempts, agent.run_stream_chunks,
  agent.schedule_occurrences TO aiagent_api;

-- Runtime worker: claims/state, reads HEAD; never writes HEAD, ledger, approvals or framework schema.
GRANT SELECT ON agent.tasks, agent.task_goal_revisions, agent.run_checkpoints, agent.tool_intents,
  agent.approval_records TO aiagent_runtime;
GRANT SELECT ON agent.runs TO aiagent_runtime;
-- Column grants: the worker owns lease/state/derived-cache pointer; it can NOT touch budget, model policy, HEAD
-- pointers or restore generation (those belong to the gateways / operator).
-- The runtime may revoke a run's authority on membership/workspace disablement.
-- It MUST NOT gain UPDATE on the owner approval decision or Tool Effect Ledger.
GRANT UPDATE (status_code, cancel_requested, lease_epoch, lease_owner_ref, lease_expires_at, heartbeat_at,
  started_at, ended_at, framework_ref, result_text, error_code, record_version, updated_at)
  ON agent.runs TO aiagent_runtime;
GRANT UPDATE (status_code, active_run_id, record_version, updated_at) ON agent.tasks TO aiagent_runtime;
GRANT SELECT, UPDATE ON queue.jobs TO aiagent_runtime;
GRANT SELECT ON queue.queues TO aiagent_runtime;
GRANT SELECT, INSERT, UPDATE ON queue.job_attempts TO aiagent_runtime;
GRANT SELECT, INSERT, UPDATE ON queue.dead_letters TO aiagent_runtime;

-- Framework checkpointer login (LangGraph AsyncPostgresSaver): DML on its own schema only.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA lg_checkpoint TO aiagent_checkpointer;
REVOKE INSERT, UPDATE, DELETE ON lg_checkpoint.checkpoint_migrations FROM aiagent_checkpointer;

-- Model Gateway: admission reads, HEAD append for model answers/summaries, budget columns, attempts/usage.
GRANT SELECT ON agent.tasks, agent.task_goal_revisions, agent.runs, agent.run_checkpoints TO aiagent_model_gateway;
GRANT UPDATE (head_revision, current_checkpoint_id, tokens_reserved, tokens_used, model_calls, updated_at)
  ON agent.runs TO aiagent_model_gateway;
GRANT INSERT ON agent.run_checkpoints, agent.run_stream_chunks, agent.cost_usage_records TO aiagent_model_gateway;
GRANT SELECT, INSERT, UPDATE ON agent.model_attempts TO aiagent_model_gateway;

-- Tool Gateway: Intent / Approval / Effect Ledger owner, HEAD append for tool results, egress permits.
GRANT SELECT ON agent.tasks, agent.task_goal_revisions, agent.runs, agent.run_checkpoints TO aiagent_tool_gateway;
GRANT UPDATE (head_revision, current_checkpoint_id, updated_at) ON agent.runs TO aiagent_tool_gateway;
GRANT INSERT ON agent.run_checkpoints TO aiagent_tool_gateway;
GRANT SELECT, INSERT, UPDATE ON agent.tool_intents, agent.approval_records, agent.tool_attempts,
  agent.side_effect_records TO aiagent_tool_gateway;
GRANT SELECT ON agent.egress_permits, agent.egress_receipts TO aiagent_tool_gateway;

-- Relay + Scheduler: outbox dispatch, queue publish/maintenance, schedule occurrences -> Task/Run creation.
GRANT SELECT, UPDATE ON agent.outbox_messages TO aiagent_relay;
GRANT SELECT, INSERT, UPDATE ON queue.jobs, queue.job_attempts, queue.dead_letters TO aiagent_relay;
GRANT SELECT, UPDATE ON queue.queues TO aiagent_relay;
GRANT SELECT, UPDATE ON agent.schedules TO aiagent_relay;
GRANT SELECT, INSERT ON agent.schedule_occurrences, agent.task_goal_revisions, agent.run_checkpoints TO aiagent_relay;
GRANT SELECT, INSERT ON agent.tasks, agent.runs TO aiagent_relay;
GRANT UPDATE (active_run_id, record_version, updated_at) ON agent.tasks TO aiagent_relay;
GRANT UPDATE (head_revision, current_checkpoint_id, updated_at) ON agent.runs TO aiagent_relay;

-- Sentinel: permit verification at send time + receipts. Read-only on the ledger.
GRANT SELECT ON agent.tool_intents, agent.approval_records, agent.runs, agent.tasks TO aiagent_sentinel;
GRANT SELECT, INSERT ON agent.egress_permits TO aiagent_sentinel;
GRANT UPDATE (uses, revoked_at) ON agent.egress_permits TO aiagent_sentinel;
GRANT INSERT ON agent.egress_receipts TO aiagent_sentinel;

-- Ops: read everything, restore incarnation / dispatch pause, DLQ decisions, retention GC.
GRANT SELECT ON ALL TABLES IN SCHEMA agent, queue TO aiagent_ops;
GRANT UPDATE (restore_incarnation, dispatch_paused, changed_at) ON agent.schema_control TO aiagent_ops;
GRANT UPDATE ON queue.queues, queue.jobs, queue.dead_letters TO aiagent_ops;
GRANT EXECUTE ON FUNCTION lg_checkpoint.gc(interval) TO aiagent_ops;

-- Retention GC for HEAD entries of terminal runs (privacy retention). Only path allowed to delete append-only rows.
CREATE FUNCTION agent.gc_terminal_runs(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  PERFORM set_config('aiagent.retention_gc', 'on', true);
  DELETE FROM agent.run_stream_chunks s USING agent.runs r
   WHERE s.tenant_id = r.tenant_id AND s.run_id = r.id
     AND r.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
     AND r.ended_at < now() - older_than;
  GET DIAGNOSTICS n = ROW_COUNT;
  PERFORM set_config('aiagent.retention_gc', 'off', true);
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION agent.gc_terminal_runs(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.gc_terminal_runs(interval) TO aiagent_ops;

-- Bearer token -> (tenant, user, workspace) before any tenant is known. Narrow SECURITY DEFINER function: the API
-- role cannot list credentials or memberships, it can only resolve a token hash it already holds.
CREATE FUNCTION agent.resolve_api_token(p_token_sha256 text)
RETURNS TABLE(tenant_id uuid, user_id uuid, workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT c.tenant_id, c.user_id, c.workspace_id FROM agent.api_credentials c
  JOIN agent.tenant_memberships m ON m.tenant_id = c.tenant_id AND m.user_id = c.user_id
  JOIN agent.tenants t ON t.id = c.tenant_id
  JOIN agent.workspaces w ON w.tenant_id = c.tenant_id AND w.id = c.workspace_id
  WHERE c.token_sha256 = p_token_sha256 AND c.revoked_at IS NULL AND (c.expires_at IS NULL OR c.expires_at > now())
    AND m.disabled_at IS NULL AND t.disabled_at IS NULL AND w.disabled_at IS NULL
$$;
REVOKE ALL ON FUNCTION agent.resolve_api_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.resolve_api_token(text) TO aiagent_api;

-- ------------------------------------------------------------------ Row Level Security
-- API connections run `SELECT set_config('aiagent.tenant_id', <authenticated tenant>, true)` inside each
-- transaction; without it every tenant-scoped row is invisible (fail closed). Service roles see all tenants
-- but every service query is tenant-predicated and fence-checked (tests: test_tenant_isolation_*).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenant_memberships','workspaces','tasks','task_goal_revisions','runs','run_checkpoints',
    'event_aggregate_counters','tenant_event_counters','domain_events','tool_intents','approval_records',
    'tool_attempts','side_effect_records','model_attempts','run_stream_chunks','cost_usage_records',
    'outbox_messages','api_idempotency','audit_records','schedules','schedule_occurrences','egress_permits'] LOOP
    EXECUTE format('ALTER TABLE agent.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format($p$CREATE POLICY api_tenant ON agent.%I TO aiagent_api
      USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)$p$, t);
    EXECUTE format($p$CREATE POLICY service_all ON agent.%I TO aiagent_runtime, aiagent_model_gateway,
      aiagent_tool_gateway, aiagent_relay, aiagent_sentinel, aiagent_ops USING (true) WITH CHECK (true)$p$, t);
  END LOOP;
END $$;
