-- Reconcile only from the canonical execution and published artifact ledger.
-- An unproven result is fenced UNKNOWN and never re-executed automatically.
CREATE FUNCTION agent.reconcile_scip_execution(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_intent uuid
) RETURNS TABLE(result_state text, artifact_id uuid)
LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE; a agent.execution_artifacts%ROWTYPE; matching integer;
BEGIN
  SELECT count(*) INTO matching FROM agent.execution_instances x
    JOIN agent.tool_intents i ON i.tenant_id=x.tenant_id AND i.id=x.tool_intent_id
    JOIN agent.tool_attempts t ON t.tenant_id=x.tenant_id AND t.id=x.tool_attempt_id
    JOIN agent.execution_recipe_approvals r ON r.tenant_id=x.tenant_id AND r.id=x.recipe_id AND r.kind='index'
    WHERE x.tenant_id=p_tenant AND x.owner_user_id=p_owner AND x.task_id=p_task AND x.run_id=p_run
      AND x.tool_intent_id=p_intent AND i.status_code IN ('SENDING','UNKNOWN')
      AND t.status_code IN ('SENDING','UNKNOWN');
  IF matching <> 1 THEN RETURN QUERY SELECT 'UNKNOWN'::text, NULL::uuid; RETURN; END IF;
  SELECT x.* INTO e FROM agent.execution_instances x
    JOIN agent.tool_intents i ON i.tenant_id=x.tenant_id AND i.id=x.tool_intent_id
    JOIN agent.tool_attempts t ON t.tenant_id=x.tenant_id AND t.id=x.tool_attempt_id
    JOIN agent.execution_recipe_approvals r ON r.tenant_id=x.tenant_id AND r.id=x.recipe_id AND r.kind='index'
    WHERE x.tenant_id=p_tenant AND x.owner_user_id=p_owner AND x.task_id=p_task AND x.run_id=p_run
      AND x.tool_intent_id=p_intent AND i.status_code IN ('SENDING','UNKNOWN')
      AND t.status_code IN ('SENDING','UNKNOWN') FOR UPDATE OF x;
  IF e.id IS NULL THEN RETURN QUERY SELECT 'UNKNOWN'::text, NULL::uuid; RETURN; END IF;
  IF e.state = 'PUBLISHED' THEN
    SELECT * INTO a FROM agent.execution_artifacts
      WHERE tenant_id=p_tenant AND execution_id=e.id AND owner_user_id=p_owner
        AND revoked_at IS NULL AND expires_at > clock_timestamp();
    IF a.id IS NOT NULL AND a.plaintext_sha256=e.artifact_sha256 AND a.byte_length=e.artifact_bytes
       AND e.capture_sha256=e.artifact_sha256 AND e.capture_bytes=e.artifact_bytes
       AND e.capture_proof_sha256 IS NOT NULL THEN
      RETURN QUERY SELECT 'PUBLISHED'::text, a.id; RETURN;
    END IF;
  ELSIF e.state IN ('PREPARED','RUNNING','FINISHED') THEN
    UPDATE agent.execution_instances SET state='UNKNOWN' WHERE tenant_id=p_tenant AND id=e.id;
  END IF;
  RETURN QUERY SELECT 'UNKNOWN'::text, NULL::uuid;
END $$;
REVOKE ALL ON FUNCTION agent.reconcile_scip_execution(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.reconcile_scip_execution(uuid,uuid,uuid,uuid,uuid) TO aiagent_tool_gateway;

-- Return only the encrypted Artifact for a still-unsettled, scoped PUBLISHED
-- execution. The Gateway must authenticate/decrypt it before confirming.
CREATE FUNCTION agent.reconcile_scip_artifact(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_intent uuid, p_artifact uuid
) RETURNS TABLE(execution_id uuid, workspace_id uuid, artifact_id uuid, plaintext_sha256 text,
                byte_length integer, coverage text, key_id text, nonce bytea, ciphertext bytea)
LANGUAGE sql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT e.id,e.workspace_id,a.id,a.plaintext_sha256::text,a.byte_length,a.coverage,a.key_id,a.nonce,a.ciphertext
  FROM agent.execution_instances e
  JOIN agent.tool_intents i ON i.tenant_id=e.tenant_id AND i.id=e.tool_intent_id
  JOIN agent.tool_attempts t ON t.tenant_id=e.tenant_id AND t.id=e.tool_attempt_id
  JOIN agent.execution_recipe_approvals r ON r.tenant_id=e.tenant_id AND r.id=e.recipe_id AND r.kind='index'
  JOIN agent.execution_artifacts a ON a.tenant_id=e.tenant_id AND a.execution_id=e.id
  WHERE e.tenant_id=p_tenant AND e.owner_user_id=p_owner AND e.task_id=p_task AND e.run_id=p_run
    AND e.tool_intent_id=p_intent AND e.state='PUBLISHED' AND i.status_code IN ('SENDING','UNKNOWN')
    AND t.status_code IN ('SENDING','UNKNOWN') AND a.id=p_artifact AND a.owner_user_id=p_owner
    AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
    AND a.plaintext_sha256=e.artifact_sha256 AND a.byte_length=e.artifact_bytes
    AND e.capture_sha256=e.artifact_sha256 AND e.capture_bytes=e.artifact_bytes
    AND e.capture_proof_sha256 IS NOT NULL
$$;
REVOKE ALL ON FUNCTION agent.reconcile_scip_artifact(uuid,uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.reconcile_scip_artifact(uuid,uuid,uuid,uuid,uuid,uuid) TO aiagent_tool_gateway;
