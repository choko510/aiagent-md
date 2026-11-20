-- Generic command evidence is distinct from SCIP Artifact storage. Raw command
-- output is never stored here; the signed body contains bounded outcome hashes.
CREATE TABLE agent.execution_command_receipts (
  tenant_id uuid NOT NULL,
  execution_id uuid NOT NULL,
  receipt_id uuid NOT NULL,
  cycle_id text NOT NULL,
  snapshot_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('build','static-check','test')),
  test_id text,
  body bytea NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 16384),
  proof bytea NOT NULL CHECK (octet_length(proof) = 64),
  body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  proof_sha256 char(64) NOT NULL CHECK (proof_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, execution_id),
  UNIQUE (tenant_id, receipt_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES agent.execution_instances(tenant_id, id),
  CHECK ((kind = 'test') = (test_id IS NOT NULL))
);
CREATE UNIQUE INDEX command_test_once_per_snapshot ON agent.execution_command_receipts
  (tenant_id, cycle_id, snapshot_id, test_id) WHERE test_id IS NOT NULL;
ALTER TABLE agent.execution_command_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.execution_command_receipts FROM PUBLIC;

CREATE FUNCTION agent.execution_finish_command(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid,
  p_binding text, p_receipt uuid, p_kind text, p_test_id text, p_body bytea, p_proof bytea,
  p_body_sha text, p_proof_sha text
) RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE; r agent.execution_recipe_approvals%ROWTYPE;
        prior agent.execution_command_receipts%ROWTYPE;
BEGIN
  IF p_kind NOT IN ('build','static-check','test') OR p_body_sha !~ '^[0-9a-f]{64}$'
     OR p_proof_sha !~ '^[0-9a-f]{64}$' OR octet_length(p_body) NOT BETWEEN 1 AND 16384
     OR octet_length(p_proof) <> 64 OR convert_from(p_proof,'UTF8') !~ '^[0-9a-f]{64}$'
  THEN RAISE EXCEPTION 'COMMAND_RECEIPT_INVALID' USING ERRCODE='check_violation'; END IF;
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  SELECT * INTO r FROM agent.execution_recipe_approvals WHERE tenant_id=p_tenant AND id=e.recipe_id;
  IF e.binding_sha256 <> p_binding OR r.id IS NULL OR r.kind <> p_kind OR coalesce(r.test_id,'') <> p_test_id THEN
    RAISE EXCEPTION 'COMMAND_BINDING_STALE' USING ERRCODE='insufficient_privilege';
  END IF;
  IF e.state='FINISHED' THEN
    SELECT * INTO prior FROM agent.execution_command_receipts WHERE tenant_id=p_tenant AND execution_id=e.id;
    IF prior.receipt_id IS NOT NULL AND prior.body=p_body AND prior.proof=p_proof
       AND prior.body_sha256=p_body_sha AND prior.proof_sha256=p_proof_sha THEN RETURN prior.receipt_id; END IF;
  END IF;
  IF e.state <> 'RUNNING' OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id
    WHERE a.tenant_id=p_tenant AND a.id=e.tool_attempt_id AND a.status_code='SENDING' AND i.status_code='SENDING'
  ) THEN RAISE EXCEPTION 'COMMAND_EXECUTION_NOT_RUNNING' USING ERRCODE='check_violation'; END IF;
  INSERT INTO agent.execution_command_receipts(tenant_id,execution_id,receipt_id,cycle_id,snapshot_id,kind,test_id,
                                                body,proof,body_sha256,proof_sha256)
    VALUES(p_tenant,e.id,p_receipt,r.cycle_id,e.snapshot_id,p_kind,nullif(p_test_id,''),
           p_body,p_proof,p_body_sha,p_proof_sha);
  UPDATE agent.execution_instances SET state='FINISHED',capture_sha256=p_body_sha,capture_bytes=octet_length(p_body),
    capture_proof_sha256=p_proof_sha WHERE tenant_id=p_tenant AND id=e.id;
  RETURN p_receipt;
END $$;
REVOKE ALL ON FUNCTION agent.execution_finish_command(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,uuid,text,text,bytea,bytea,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_finish_command(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,uuid,text,text,bytea,bytea,text,text) TO aiagent_tool_gateway;

CREATE FUNCTION agent.execution_fetch_command(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_execution uuid
) RETURNS TABLE(receipt_id uuid,kind text,test_id text,body bytea,proof bytea,body_sha256 text,proof_sha256 text)
LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e agent.execution_instances%ROWTYPE;
BEGIN
  e := agent.execution_artifact_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_execution);
  IF e.state <> 'FINISHED' THEN RAISE EXCEPTION 'COMMAND_RECEIPT_UNAVAILABLE' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN QUERY SELECT r.receipt_id,r.kind,r.test_id,r.body,r.proof,r.body_sha256::text,r.proof_sha256::text
    FROM agent.execution_command_receipts r WHERE r.tenant_id=p_tenant AND r.execution_id=e.id
      AND r.body_sha256=e.capture_sha256 AND octet_length(r.body)=e.capture_bytes AND r.proof_sha256=e.capture_proof_sha256;
END $$;
REVOKE ALL ON FUNCTION agent.execution_fetch_command(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.execution_fetch_command(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) TO aiagent_tool_gateway;
