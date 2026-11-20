-- A narrow locator only; the Gateway must still re-read and authenticate the
-- receipt under the original live Run/Lease before confirming any outcome.
CREATE FUNCTION agent.command_reconcile_locator(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_intent uuid
) RETURNS TABLE(execution_id uuid, receipt_id uuid)
LANGUAGE sql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT e.id, r.receipt_id FROM agent.execution_instances e
  JOIN agent.execution_command_receipts r ON r.tenant_id=e.tenant_id AND r.execution_id=e.id
  JOIN agent.tool_intents i ON i.tenant_id=e.tenant_id AND i.id=e.tool_intent_id
  JOIN agent.tool_attempts a ON a.tenant_id=e.tenant_id AND a.id=e.tool_attempt_id
  JOIN agent.execution_recipe_approvals p ON p.tenant_id=e.tenant_id AND p.id=e.recipe_id
  WHERE e.tenant_id=p_tenant AND e.owner_user_id=p_owner AND e.task_id=p_task AND e.run_id=p_run
    AND e.tool_intent_id=p_intent AND e.state='FINISHED' AND i.status_code='SENDING' AND a.status_code='SENDING'
    AND p.kind IN ('build','static-check','test') AND r.kind=p.kind AND r.test_id IS NOT DISTINCT FROM p.test_id
    AND r.snapshot_id=e.snapshot_id AND r.cycle_id=p.cycle_id AND r.body_sha256=e.capture_sha256
    AND octet_length(r.body)=e.capture_bytes AND r.proof_sha256=e.capture_proof_sha256
$$;
REVOKE ALL ON FUNCTION agent.command_reconcile_locator(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.command_reconcile_locator(uuid,uuid,uuid,uuid,uuid) TO aiagent_tool_gateway;
