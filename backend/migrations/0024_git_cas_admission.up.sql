-- The transition is an irrevocable *admission*, not proof of a Git ref update.
-- A caller must first authenticate the Worker's durable PREPARED journal.
-- Any interruption after this transition remains UNKNOWN and is not retried.
ALTER TABLE agent.git_operations DROP CONSTRAINT git_operations_state_check;
ALTER TABLE agent.git_operations ADD CONSTRAINT git_operations_state_check
  CHECK (state IN ('ADMITTED','CAS_PENDING','RECEIPT_PERSISTED','UNKNOWN'));
ALTER TABLE agent.git_operations ADD COLUMN candidate_commit text;
ALTER TABLE agent.git_operations ADD COLUMN candidate_tree text;
ALTER TABLE agent.git_operations ADD COLUMN prepared_journal_sha256 text;
ALTER TABLE agent.git_operations ADD CONSTRAINT git_cas_candidate_consistent CHECK (
  (state <> 'CAS_PENDING' AND candidate_commit IS NULL AND candidate_tree IS NULL AND prepared_journal_sha256 IS NULL)
  OR (candidate_commit IS NOT NULL AND candidate_tree IS NOT NULL AND prepared_journal_sha256 IS NOT NULL
      AND candidate_commit ~ '^[0-9a-f]{40}$' AND candidate_tree ~ '^[0-9a-f]{40}$'
      AND prepared_journal_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE FUNCTION agent.git_operation_enter_cas(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid,
  p_commit text, p_tree text, p_journal_sha text
) RETURNS void LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE o agent.git_operations%ROWTYPE;
BEGIN
  IF p_commit !~ '^[0-9a-f]{40}$' OR p_tree !~ '^[0-9a-f]{40}$' OR p_journal_sha !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'INVALID_GIT_CANDIDATE' USING ERRCODE='check_violation';
  END IF;
  o := agent.git_operation_receipt_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_operation);
  IF o.state<>'ADMITTED' OR o.parent_sha=p_commit OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id JOIN agent.side_effect_records e
      ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id
    WHERE a.tenant_id=p_tenant AND a.id=o.attempt_id AND a.status_code='SENDING'
      AND i.status_code='SENDING' AND e.outcome_code='SENDING'
  ) THEN RAISE EXCEPTION 'GIT_CAS_NOT_ADMITTED' USING ERRCODE='insufficient_privilege'; END IF;
  UPDATE agent.git_operations SET state='CAS_PENDING',candidate_commit=p_commit,candidate_tree=p_tree,
    prepared_journal_sha256=p_journal_sha WHERE tenant_id=p_tenant AND id=o.id;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_enter_cas(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_enter_cas(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,text,text,text) TO aiagent_tool_gateway;

-- Fenced receipts require the exact journal-bound CAS candidate.
CREATE FUNCTION agent.git_operation_finish_fenced(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid,
  p_receipt uuid, p_body bytea, p_proof bytea, p_body_sha text, p_proof_sha text
) RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE o agent.git_operations%ROWTYPE; prior agent.git_operation_receipts%ROWTYPE; payload jsonb;
BEGIN
  IF octet_length(p_body) NOT BETWEEN 1 AND 16384 OR octet_length(p_proof)<>64
     OR convert_from(p_proof,'UTF8') !~ '^[0-9a-f]{64}$'
     OR p_body_sha !~ '^[0-9a-f]{64}$' OR p_proof_sha !~ '^[0-9a-f]{64}$'
  THEN RAISE EXCEPTION 'GIT_RECEIPT_INVALID' USING ERRCODE='check_violation'; END IF;
  o := agent.git_operation_receipt_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_operation);
  payload := convert_from(p_body,'UTF8')::jsonb;
  IF jsonb_typeof(payload) IS DISTINCT FROM 'object'
     OR payload->>'operation' IS DISTINCT FROM o.id::text OR payload->>'tenant' IS DISTINCT FROM p_tenant::text
     OR payload->>'owner' IS DISTINCT FROM p_owner::text OR payload->>'workspace' IS DISTINCT FROM p_workspace::text
     OR payload->>'task' IS DISTINCT FROM p_task::text OR payload->>'run' IS DISTINCT FROM p_run::text
     OR payload->>'goal' IS DISTINCT FROM p_goal::text OR payload->>'lease' IS DISTINCT FROM p_lease::text
     OR payload->>'restore' IS DISTINCT FROM p_restore::text OR payload->>'worker_ref' IS DISTINCT FROM p_worker
     OR payload->>'intent' IS DISTINCT FROM o.intent_id::text OR payload->>'attempt' IS DISTINCT FROM o.attempt_id::text
     OR payload->>'effect_key' IS DISTINCT FROM o.effect_key OR payload->>'binding' IS DISTINCT FROM o.binding_id::text
     OR payload->>'generation' IS DISTINCT FROM o.binding_generation::text OR payload->>'branch' IS DISTINCT FROM o.branch
     OR payload->>'patch_id' IS DISTINCT FROM o.patch_id::text OR payload->>'patch_sha256' IS DISTINCT FROM o.patch_sha256
     OR payload->>'cycle_id' IS DISTINCT FROM o.cycle_id OR payload->>'plan_ref' IS DISTINCT FROM o.plan_ref
     OR payload->>'parent' IS DISTINCT FROM o.parent_sha OR payload->>'request_sha256' IS DISTINCT FROM o.request_sha256
     OR payload->>'cas' IS DISTINCT FROM 'CONFIRMED' OR coalesce(payload->>'commit','') !~ '^[0-9a-f]{40}$'
     OR coalesce(payload->>'tree','') !~ '^[0-9a-f]{40}$' OR coalesce(length(payload->>'worker_id'),0)=0
  THEN RAISE EXCEPTION 'GIT_RECEIPT_SCOPE_MISMATCH' USING ERRCODE='insufficient_privilege'; END IF;
  IF o.state='RECEIPT_PERSISTED' THEN
    SELECT * INTO prior FROM agent.git_operation_receipts WHERE tenant_id=p_tenant AND operation_id=o.id;
    IF prior.receipt_id IS NOT NULL AND prior.body=p_body AND prior.proof=p_proof
       AND prior.body_sha256=p_body_sha AND prior.proof_sha256=p_proof_sha THEN RETURN prior.receipt_id; END IF;
  END IF;
  IF o.state<>'CAS_PENDING' OR payload->>'commit' IS DISTINCT FROM o.candidate_commit
     OR payload->>'tree' IS DISTINCT FROM o.candidate_tree OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id WHERE a.tenant_id=p_tenant AND a.id=o.attempt_id
      AND a.status_code='SENDING' AND i.status_code='SENDING'
  ) THEN RAISE EXCEPTION 'GIT_OPERATION_NOT_ADMITTED' USING ERRCODE='check_violation'; END IF;
  INSERT INTO agent.git_operation_receipts(tenant_id,operation_id,receipt_id,body,proof,body_sha256,proof_sha256)
    VALUES(p_tenant,o.id,p_receipt,p_body,p_proof,p_body_sha,p_proof_sha);
  UPDATE agent.git_operations SET state='RECEIPT_PERSISTED' WHERE tenant_id=p_tenant AND id=o.id;
  RETURN p_receipt;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_finish_fenced(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_finish_fenced(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text) TO aiagent_tool_gateway;
