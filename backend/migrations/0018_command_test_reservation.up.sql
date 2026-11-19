-- Reserve each planned Test ID before any remote Source Session can start.
-- Reservations are permanent for the same snapshot/cycle: an uncertain run is
-- never made reusable by expiry or by deleting an execution receipt.
CREATE TABLE agent.execution_test_reservations (
  tenant_id uuid NOT NULL,
  cycle_id text NOT NULL,
  snapshot_id uuid NOT NULL,
  test_id text NOT NULL CHECK (length(test_id) BETWEEN 1 AND 256),
  execution_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, cycle_id, snapshot_id, test_id),
  UNIQUE (tenant_id, execution_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES agent.execution_instances(tenant_id, id)
);
ALTER TABLE agent.execution_test_reservations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.execution_test_reservations FROM PUBLIC;

-- Fence concurrent old-version issuers while carrying forward all already
-- issued test sessions. Conflicting historical attempts abort migration rather
-- than silently selecting one as authoritative.
LOCK TABLE agent.execution_instances, agent.execution_source_sessions IN SHARE ROW EXCLUSIVE MODE;
INSERT INTO agent.execution_test_reservations(tenant_id,cycle_id,snapshot_id,test_id,execution_id)
  SELECT e.tenant_id,r.cycle_id,e.snapshot_id,r.test_id,e.id
  FROM agent.execution_instances e
  JOIN agent.execution_source_sessions s ON s.tenant_id=e.tenant_id AND s.execution_id=e.id
  JOIN agent.execution_recipe_approvals r ON r.tenant_id=e.tenant_id AND r.id=e.recipe_id
  WHERE r.kind='test';

CREATE OR REPLACE FUNCTION agent.execution_issue_source_session(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint,
  p_execution uuid, p_binding text, p_session uuid, p_expires timestamptz
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE; recipe agent.execution_recipe_approvals%ROWTYPE;
BEGIN
  IF p_expires <= clock_timestamp() OR p_expires > clock_timestamp() + interval '60 seconds' THEN
    RAISE EXCEPTION 'INVALID_SOURCE_SESSION_EXPIRY' USING ERRCODE='check_violation';
  END IF;
  PERFORM agent.execution_start(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,
                                p_execution,p_binding);
  SELECT * INTO e FROM agent.execution_instances WHERE tenant_id=p_tenant AND id=p_execution;
  SELECT * INTO recipe FROM agent.execution_recipe_approvals WHERE tenant_id=p_tenant AND id=e.recipe_id;
  IF recipe.kind='test' THEN
    INSERT INTO agent.execution_test_reservations(tenant_id,cycle_id,snapshot_id,test_id,execution_id)
      VALUES(p_tenant,recipe.cycle_id,e.snapshot_id,recipe.test_id,e.id);
  END IF;
  INSERT INTO agent.execution_source_sessions(tenant_id,execution_id,session_id,expires_at)
    VALUES (p_tenant,p_execution,p_session,p_expires);
END $$;
