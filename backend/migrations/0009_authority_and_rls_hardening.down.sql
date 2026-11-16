-- Revert 0009_authority_and_rls_hardening back to 0007/0008 baseline.

DROP POLICY IF EXISTS browser_session_ops_scope ON agent.browser_sessions;

DROP POLICY IF EXISTS dev_cycles_ops_scope ON agent.dev_cycles;
DROP POLICY IF EXISTS dev_cycles_api_scope ON agent.dev_cycles;
CREATE POLICY dev_cycles_api_scope ON agent.dev_cycles TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP POLICY IF EXISTS dev_diff_claims_ops_scope ON agent.dev_diff_claims;
DROP POLICY IF EXISTS dev_diff_claims_api_scope ON agent.dev_diff_claims;
CREATE POLICY dev_diff_claims_api_scope ON agent.dev_diff_claims TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP POLICY IF EXISTS preview_leases_ops_scope ON agent.preview_leases;
DROP POLICY IF EXISTS preview_leases_api_scope ON agent.preview_leases;
CREATE POLICY preview_leases_api_scope ON agent.preview_leases TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP POLICY IF EXISTS automation_campaigns_ops_scope ON agent.automation_campaigns;
DROP POLICY IF EXISTS automation_campaigns_api_scope ON agent.automation_campaigns;
CREATE POLICY automation_campaigns_api_scope ON agent.automation_campaigns TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP POLICY IF EXISTS automation_watches_ops_scope ON agent.automation_watches;
DROP POLICY IF EXISTS automation_watches_api_scope ON agent.automation_watches;
CREATE POLICY automation_watches_api_scope ON agent.automation_watches TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP POLICY IF EXISTS automation_watch_slots_ops_scope ON agent.automation_watch_slots;
DROP POLICY IF EXISTS automation_watch_slots_api_scope ON agent.automation_watch_slots;
CREATE POLICY automation_watch_slots_api_scope ON agent.automation_watch_slots TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.tenant_id', true));

DROP FUNCTION IF EXISTS agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text);
DROP FUNCTION IF EXISTS agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text);
DROP FUNCTION IF EXISTS agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text, text);
DROP FUNCTION IF EXISTS agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text);

CREATE FUNCTION agent.browser_session_register(
  p_tenant uuid, p_session uuid, p_owner uuid, p_task uuid, p_run uuid, p_goal bigint, p_lease bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT EXISTS (
    SELECT 1 FROM agent.tasks t JOIN agent.runs r ON r.tenant_id = t.tenant_id AND r.id = p_run
     WHERE t.tenant_id = p_tenant AND t.id = p_task AND t.owner_user_id = p_owner
       AND t.goal_revision = p_goal AND t.active_run_id = p_run AND r.task_id = t.id
       AND r.lease_epoch = p_lease AND r.restore_incarnation = v_incarnation
       AND NOT r.cancel_requested AND NOT r.pause_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
  ) THEN RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation'; END IF;
  INSERT INTO agent.browser_sessions(tenant_id, id, owner_user_id, task_id, run_id, goal_revision, lease_epoch, mode, state, generation, restore_incarnation)
  VALUES (p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, 'AGENT', 'ACTIVE', 1, v_incarnation);
END $$;

CREATE FUNCTION agent.browser_session_check(
  p_tenant uuid, p_session uuid, p_owner uuid, p_task uuid, p_run uuid, p_goal bigint, p_lease bigint, p_mode text DEFAULT 'AGENT'
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM agent.browser_sessions s JOIN agent.tasks t ON t.tenant_id = s.tenant_id AND t.id = s.task_id
      JOIN agent.runs r ON r.tenant_id = s.tenant_id AND r.id = s.run_id JOIN agent.schema_control sc ON sc.singleton
     WHERE s.tenant_id = p_tenant AND s.id = p_session AND s.owner_user_id = p_owner AND s.task_id = p_task
       AND s.run_id = p_run AND s.goal_revision = p_goal AND s.lease_epoch = p_lease AND s.mode = p_mode
       AND s.closed_at IS NULL AND s.state <> 'CLOSED' AND s.restore_incarnation = sc.restore_incarnation
       AND t.owner_user_id = p_owner AND t.goal_revision = p_goal AND r.restore_incarnation = sc.restore_incarnation
       AND NOT r.cancel_requested AND NOT r.pause_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','PAUSED','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
  ) THEN RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation'; END IF;
END $$;

CREATE FUNCTION agent.browser_session_advance(
  p_tenant uuid, p_session uuid, p_owner uuid, p_task uuid, p_old_run uuid, p_old_goal bigint, p_old_lease bigint,
  p_new_run uuid, p_new_goal bigint, p_new_lease bigint, p_mode text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE v_rows integer; v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  UPDATE agent.browser_sessions s SET run_id = p_new_run, goal_revision = p_new_goal, lease_epoch = p_new_lease, mode = p_mode, updated_at = now()
    FROM agent.tasks t, agent.runs r
   WHERE s.tenant_id = p_tenant AND s.id = p_session AND s.owner_user_id = p_owner AND s.task_id = p_task
     AND s.run_id = p_old_run AND s.goal_revision = p_old_goal AND s.lease_epoch = p_old_lease AND s.mode = 'AGENT'
     AND s.closed_at IS NULL AND s.state <> 'CLOSED' AND s.restore_incarnation = v_incarnation
     AND t.tenant_id = p_tenant AND t.id = p_task AND t.owner_user_id = p_owner AND t.goal_revision = p_new_goal
     AND r.tenant_id = p_tenant AND r.id = p_new_run AND r.task_id = p_task AND r.restore_incarnation = v_incarnation
     AND agent.scope_active(p_tenant, p_owner, t.workspace_id);
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation'; END IF;
END $$;

CREATE FUNCTION agent.browser_session_revoke(
  p_tenant uuid, p_session uuid, p_owner uuid, p_task uuid, p_run uuid, p_goal bigint, p_lease bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE v_rows integer;
BEGIN
  UPDATE agent.browser_sessions s SET state = 'CLOSED', closed_at = now(), updated_at = now()
   WHERE s.tenant_id = p_tenant AND s.id = p_session AND s.owner_user_id = p_owner AND s.task_id = p_task
     AND s.run_id = p_run AND s.goal_revision = p_goal AND s.lease_epoch = p_lease AND s.closed_at IS NULL;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation'; END IF;
END $$;

REVOKE ALL ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.browser_session_register(uuid, uuid, uuid, uuid, uuid, bigint, bigint) TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_check(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text) TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_advance(uuid, uuid, uuid, uuid, uuid, bigint, bigint, uuid, bigint, bigint, text) TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_revoke(uuid, uuid, uuid, uuid, uuid, bigint, bigint) TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;

ALTER TABLE agent.browser_sessions DROP COLUMN IF EXISTS worker_ref;
