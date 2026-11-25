CREATE OR REPLACE FUNCTION agent.execution_issue_source_session(
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
DROP TABLE agent.execution_test_reservations;
