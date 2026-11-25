-- A heartbeat only follows a fresh canonical scope and execution check.
CREATE FUNCTION agent.execution_check_running(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid, p_binding text
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE;
BEGIN
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.state <> 'RUNNING' OR e.binding_sha256 <> p_binding OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id
    WHERE a.tenant_id=p_tenant AND a.id=e.tool_attempt_id
      AND a.status_code='SENDING' AND i.status_code='SENDING'
  ) THEN
    RAISE EXCEPTION 'EXECUTION_NOT_RUNNING' USING ERRCODE='insufficient_privilege';
  END IF;
END $$;
REVOKE ALL ON FUNCTION agent.execution_check_running(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_check_running(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text) TO aiagent_tool_gateway;
