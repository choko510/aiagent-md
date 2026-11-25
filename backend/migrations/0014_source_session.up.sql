-- One issuance per canonical Execution, committed atomically with RUNNING.
-- A lost response must not be retried as a fresh session.
CREATE TABLE agent.execution_source_sessions (
  tenant_id uuid NOT NULL,
  execution_id uuid NOT NULL,
  session_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, execution_id),
  UNIQUE (session_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES agent.execution_instances(tenant_id, id)
);
ALTER TABLE agent.execution_source_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.execution_source_sessions FROM PUBLIC;

CREATE FUNCTION agent.execution_issue_source_session(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint,
  p_execution uuid, p_binding text, p_session uuid, p_expires timestamptz
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF p_expires <= clock_timestamp() OR p_expires > clock_timestamp() + interval '60 seconds' THEN
    RAISE EXCEPTION 'INVALID_SOURCE_SESSION_EXPIRY' USING ERRCODE='check_violation';
  END IF;
  PERFORM agent.execution_start(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,
                                p_execution,p_binding);
  INSERT INTO agent.execution_source_sessions(tenant_id,execution_id,session_id,expires_at)
    VALUES (p_tenant,p_execution,p_session,p_expires);
END $$;
REVOKE ALL ON FUNCTION agent.execution_issue_source_session(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,uuid,timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_issue_source_session(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,uuid,timestamptz) TO aiagent_tool_gateway;

-- Fail closed even if the original lease has since expired. This operation can
-- only remove execution authority, never create it or undo publication.
CREATE FUNCTION agent.execution_mark_unknown(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_execution uuid, p_binding text
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE current_state text;
BEGIN
  UPDATE agent.execution_instances SET state='UNKNOWN'
    WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task AND run_id=p_run
      AND id=p_execution AND binding_sha256=p_binding AND state IN ('PREPARED','RUNNING','FINISHED');
  IF FOUND THEN RETURN; END IF;
  SELECT state INTO current_state FROM agent.execution_instances
    WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task AND run_id=p_run
      AND id=p_execution AND binding_sha256=p_binding;
  IF current_state IS DISTINCT FROM 'UNKNOWN' THEN
    RAISE EXCEPTION 'EXECUTION_UNKNOWN_UNAVAILABLE' USING ERRCODE='insufficient_privilege';
  END IF;
END $$;
REVOKE ALL ON FUNCTION agent.execution_mark_unknown(uuid,uuid,uuid,uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_mark_unknown(uuid,uuid,uuid,uuid,uuid,text) TO aiagent_tool_gateway;
