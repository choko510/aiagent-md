-- 0007_browser_sessions: Postgres-backed fenced browser sessions for ToolGateway + BrowserSessionService.

CREATE TABLE agent.browser_sessions (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  goal_revision bigint NOT NULL CHECK (goal_revision >= 1),
  lease_epoch bigint NOT NULL CHECK (lease_epoch >= 1),
  mode text NOT NULL DEFAULT 'AGENT' CHECK (mode IN ('AGENT', 'HUMAN')),
  state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('STARTING', 'ACTIVE', 'PAUSED', 'RECOVERING', 'NEEDS_LOGIN', 'CLOSED')),
  generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
  restore_incarnation bigint NOT NULL DEFAULT 1 CHECK (restore_incarnation >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent.runs(tenant_id, id),
  CHECK ((state = 'CLOSED') = (closed_at IS NOT NULL))
);

CREATE INDEX browser_sessions_scope ON agent.browser_sessions (tenant_id, owner_user_id, task_id) WHERE closed_at IS NULL;

ALTER TABLE agent.browser_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.browser_sessions FROM PUBLIC;
GRANT SELECT ON agent.browser_sessions TO aiagent_ops, aiagent_sentinel;

CREATE POLICY browser_session_api_scope ON agent.browser_sessions TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));
GRANT SELECT ON agent.browser_sessions TO aiagent_api;

CREATE FUNCTION agent.browser_session_register(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  IF NOT EXISTS (
    SELECT 1
      FROM agent.tasks t
      JOIN agent.runs r ON r.tenant_id = t.tenant_id AND r.id = p_run
     WHERE t.tenant_id = p_tenant
       AND t.id = p_task
       AND t.owner_user_id = p_owner
       AND t.goal_revision = p_goal
       AND t.active_run_id = p_run
       AND r.task_id = t.id
       AND r.lease_epoch = p_lease
       AND r.restore_incarnation = v_incarnation
       AND NOT r.cancel_requested
       AND NOT r.pause_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
  ) THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;

  INSERT INTO agent.browser_sessions(
    tenant_id, id, owner_user_id, task_id, run_id, goal_revision, lease_epoch, mode, state, generation, restore_incarnation
  ) VALUES (
    p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, 'AGENT', 'ACTIVE', 1, v_incarnation
  );
END $$;

CREATE FUNCTION agent.browser_session_check(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint,
  p_mode text DEFAULT 'AGENT'
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM agent.browser_sessions s
      JOIN agent.tasks t ON t.tenant_id = s.tenant_id AND t.id = s.task_id
      JOIN agent.runs r ON r.tenant_id = s.tenant_id AND r.id = s.run_id
      JOIN agent.schema_control sc ON sc.singleton
     WHERE s.tenant_id = p_tenant
       AND s.id = p_session
       AND s.owner_user_id = p_owner
       AND s.task_id = p_task
       AND s.run_id = p_run
       AND s.goal_revision = p_goal
       AND s.lease_epoch = p_lease
       AND s.mode = p_mode
       AND s.closed_at IS NULL
       AND s.state <> 'CLOSED'
       AND s.restore_incarnation = sc.restore_incarnation
       AND t.owner_user_id = p_owner
       AND t.goal_revision = p_goal
       AND r.restore_incarnation = sc.restore_incarnation
       AND NOT r.cancel_requested
       AND NOT r.pause_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
  ) THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
END $$;

CREATE FUNCTION agent.browser_session_advance(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_old_run uuid,
  p_old_goal bigint,
  p_old_lease bigint,
  p_new_run uuid,
  p_new_goal bigint,
  p_new_lease bigint,
  p_mode text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
  v_incarnation bigint;
BEGIN
  IF p_mode NOT IN ('AGENT', 'HUMAN') THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
  IF p_new_lease <= p_old_lease AND p_new_goal <= p_old_goal AND p_new_run = p_old_run THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;

  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  UPDATE agent.browser_sessions s
     SET run_id = p_new_run,
         goal_revision = p_new_goal,
         lease_epoch = p_new_lease,
         mode = p_mode,
         updated_at = now()
    FROM agent.tasks t, agent.runs r
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.task_id = p_task
     AND s.run_id = p_old_run
     AND s.goal_revision = p_old_goal
     AND s.lease_epoch = p_old_lease
     AND s.mode = 'AGENT'
     AND s.closed_at IS NULL
     AND s.state <> 'CLOSED'
     AND s.restore_incarnation = v_incarnation
     AND t.tenant_id = p_tenant
     AND t.id = p_task
     AND t.owner_user_id = p_owner
     AND t.goal_revision = p_new_goal
     AND r.tenant_id = p_tenant
     AND r.id = p_new_run
     AND r.task_id = p_task
     AND r.restore_incarnation = v_incarnation
     AND agent.scope_active(p_tenant, p_owner, t.workspace_id);

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
END $$;

CREATE FUNCTION agent.browser_session_revoke(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
BEGIN
  UPDATE agent.browser_sessions s
     SET state = 'CLOSED',
         closed_at = now(),
         updated_at = now()
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.task_id = p_task
     AND s.run_id = p_run
     AND s.goal_revision = p_goal
     AND s.lease_epoch = p_lease
     AND s.closed_at IS NULL;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
END $$;

REVOKE ALL ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
