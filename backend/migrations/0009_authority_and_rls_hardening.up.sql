-- 0009_authority_and_rls_hardening: enforce live Run lease expiry/ownership in Browser fences and complete RLS policies for Dev/Automation service roles.

ALTER TABLE agent.browser_sessions
  ADD COLUMN IF NOT EXISTS worker_ref text;

DROP FUNCTION IF EXISTS agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint);
DROP FUNCTION IF EXISTS agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text);
DROP FUNCTION IF EXISTS agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text);
DROP FUNCTION IF EXISTS agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint);

CREATE FUNCTION agent.browser_session_register(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint,
  p_worker text DEFAULT NULL
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
       AND r.lease_owner_ref IS NOT NULL
       AND r.lease_expires_at IS NOT NULL
       AND r.lease_expires_at > clock_timestamp()
       AND (p_worker IS NULL OR r.lease_owner_ref = p_worker)
       AND r.restore_incarnation = v_incarnation
       AND NOT r.cancel_requested
       AND NOT r.pause_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
  ) THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;

  INSERT INTO agent.browser_sessions(
    tenant_id, id, owner_user_id, task_id, run_id, goal_revision, lease_epoch, mode, state, generation, restore_incarnation, worker_ref
  ) VALUES (
    p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, 'AGENT', 'ACTIVE', 1, v_incarnation, p_worker
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
  p_mode text DEFAULT 'AGENT',
  p_worker text DEFAULT NULL
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
       AND (s.worker_ref IS NULL OR r.lease_owner_ref = s.worker_ref)
       AND t.owner_user_id = p_owner
       AND t.goal_revision = p_goal
       AND t.active_run_id = s.run_id
       AND r.lease_epoch = s.lease_epoch
       AND r.lease_owner_ref IS NOT NULL
       AND r.lease_expires_at IS NOT NULL
       AND r.lease_expires_at > clock_timestamp()
       AND (p_worker IS NULL OR r.lease_owner_ref = p_worker)
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
  p_mode text,
  p_new_worker text DEFAULT NULL
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
         worker_ref = p_new_worker,
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
     AND t.active_run_id = p_new_run
     AND r.tenant_id = p_tenant
     AND r.id = p_new_run
     AND r.task_id = p_task
     AND r.lease_epoch = p_new_lease
     AND r.lease_owner_ref IS NOT NULL
     AND r.lease_expires_at IS NOT NULL
     AND r.lease_expires_at > clock_timestamp()
     AND (p_new_worker IS NULL OR r.lease_owner_ref = p_new_worker)
     AND r.restore_incarnation = v_incarnation
     AND NOT r.cancel_requested
     AND NOT r.pause_requested
     AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
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
  p_lease bigint,
  p_worker text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
  v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  UPDATE agent.browser_sessions s
     SET state = 'CLOSED',
         closed_at = now(),
         updated_at = now()
    FROM agent.tasks t, agent.runs r
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.task_id = p_task
     AND s.run_id = p_run
     AND s.goal_revision = p_goal
     AND s.lease_epoch = p_lease
     AND s.closed_at IS NULL
     AND s.restore_incarnation = v_incarnation
     AND (s.worker_ref IS NULL OR r.lease_owner_ref = s.worker_ref)
     AND t.tenant_id = p_tenant
     AND t.id = p_task
     AND t.owner_user_id = p_owner
     AND t.active_run_id = p_run
     AND r.tenant_id = p_tenant
     AND r.id = p_run
     AND r.task_id = p_task
     AND r.lease_epoch = p_lease
     AND r.lease_owner_ref IS NOT NULL
     AND r.lease_expires_at IS NOT NULL
     AND r.lease_expires_at > clock_timestamp()
     AND (p_worker IS NULL OR r.lease_owner_ref = p_worker)
     AND r.restore_incarnation = v_incarnation;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
END $$;

REVOKE ALL ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;

-- Ensure browser_sessions ops/sentinel read visibility matches GRANT SELECT.
DROP POLICY IF EXISTS browser_session_ops_scope ON agent.browser_sessions;
CREATE POLICY browser_session_ops_scope ON agent.browser_sessions FOR SELECT TO aiagent_ops, aiagent_sentinel
  USING (true);

-- Complete tenant-scoped RLS policies for PR #97 Development and Automation roles.
DROP POLICY IF EXISTS dev_cycles_api_scope ON agent.dev_cycles;
CREATE POLICY dev_cycles_api_scope ON agent.dev_cycles TO aiagent_api, aiagent_runtime, aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS dev_cycles_ops_scope ON agent.dev_cycles;
CREATE POLICY dev_cycles_ops_scope ON agent.dev_cycles FOR SELECT TO aiagent_ops USING (true);

DROP POLICY IF EXISTS dev_diff_claims_api_scope ON agent.dev_diff_claims;
CREATE POLICY dev_diff_claims_api_scope ON agent.dev_diff_claims TO aiagent_api, aiagent_runtime, aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS dev_diff_claims_ops_scope ON agent.dev_diff_claims;
CREATE POLICY dev_diff_claims_ops_scope ON agent.dev_diff_claims FOR SELECT TO aiagent_ops USING (true);

DROP POLICY IF EXISTS preview_leases_api_scope ON agent.preview_leases;
CREATE POLICY preview_leases_api_scope ON agent.preview_leases TO aiagent_api, aiagent_runtime, aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS preview_leases_ops_scope ON agent.preview_leases;
CREATE POLICY preview_leases_ops_scope ON agent.preview_leases FOR SELECT TO aiagent_ops USING (true);

DROP POLICY IF EXISTS automation_campaigns_api_scope ON agent.automation_campaigns;
CREATE POLICY automation_campaigns_api_scope ON agent.automation_campaigns TO aiagent_api, aiagent_relay
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS automation_campaigns_ops_scope ON agent.automation_campaigns;
CREATE POLICY automation_campaigns_ops_scope ON agent.automation_campaigns FOR SELECT TO aiagent_ops USING (true);

DROP POLICY IF EXISTS automation_watches_api_scope ON agent.automation_watches;
CREATE POLICY automation_watches_api_scope ON agent.automation_watches TO aiagent_api, aiagent_relay
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS automation_watches_ops_scope ON agent.automation_watches;
CREATE POLICY automation_watches_ops_scope ON agent.automation_watches FOR SELECT TO aiagent_ops USING (true);

DROP POLICY IF EXISTS automation_watch_slots_api_scope ON agent.automation_watch_slots;
CREATE POLICY automation_watch_slots_api_scope ON agent.automation_watch_slots TO aiagent_api, aiagent_relay
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
DROP POLICY IF EXISTS automation_watch_slots_ops_scope ON agent.automation_watch_slots;
CREATE POLICY automation_watch_slots_ops_scope ON agent.automation_watch_slots FOR SELECT TO aiagent_ops USING (true);
