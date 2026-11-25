-- A heartbeat confirms only a still-SENDING operation under the original
-- authority. It never changes state or makes UNKNOWN retryable.
CREATE OR REPLACE FUNCTION agent.git_operation_heartbeat(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid
) RETURNS double precision LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE o agent.git_operations%ROWTYPE; expiry timestamptz;
BEGIN
  o := agent.git_operation_receipt_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_operation);
  IF o.state NOT IN ('ADMITTED','CAS_PENDING') OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id JOIN agent.side_effect_records e
      ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id
    WHERE a.tenant_id=p_tenant AND a.id=o.attempt_id AND a.status_code='SENDING'
      AND i.status_code='SENDING' AND e.outcome_code='SENDING'
  ) THEN RAISE EXCEPTION 'GIT_HEARTBEAT_DENIED' USING ERRCODE='insufficient_privilege'; END IF;
  SELECT least(r.lease_expires_at, a.expires_at) INTO expiry FROM agent.runs r
    JOIN agent.approval_records a ON a.tenant_id=r.tenant_id AND a.tool_intent_id=o.intent_id
    WHERE r.tenant_id=p_tenant AND r.id=p_run;
  IF expiry IS NULL OR expiry<=clock_timestamp() THEN
    RAISE EXCEPTION 'GIT_HEARTBEAT_DENIED' USING ERRCODE='insufficient_privilege';
  END IF;
  RETURN extract(epoch FROM expiry);
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_heartbeat(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_heartbeat(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) TO aiagent_tool_gateway;
