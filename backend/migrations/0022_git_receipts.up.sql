-- Git receipts are distinct from command and SCIP artifacts. The Gateway must
-- authenticate the isolated worker response before invoking this DB boundary.
ALTER TABLE agent.git_operations DROP CONSTRAINT git_operations_state_check;
ALTER TABLE agent.git_operations ADD CONSTRAINT git_operations_state_check
  CHECK (state IN ('ADMITTED', 'RECEIPT_PERSISTED', 'UNKNOWN'));

CREATE TABLE agent.git_operation_receipts (
  tenant_id uuid NOT NULL,
  operation_id uuid NOT NULL,
  receipt_id uuid NOT NULL,
  body bytea NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 16384),
  proof bytea NOT NULL CHECK (octet_length(proof) = 64),
  body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  proof_sha256 text NOT NULL CHECK (proof_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, operation_id),
  UNIQUE (tenant_id, receipt_id),
  FOREIGN KEY (tenant_id, operation_id) REFERENCES agent.git_operations(tenant_id, id)
);
ALTER TABLE agent.git_operation_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.git_operation_receipts FROM PUBLIC;

-- Lock in the same order as git_operation_admit. This function is private to
-- SECURITY DEFINER receipt operations; it does not authorize a new Git send.
CREATE FUNCTION agent.git_operation_receipt_lock(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid
) RETURNS agent.git_operations LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  t agent.tasks%ROWTYPE; r agent.runs%ROWTYPE; hint agent.git_operations%ROWTYPE;
  o agent.git_operations%ROWTYPE; b agent.repository_bindings%ROWTYPE; p agent.git_patch_artifacts%ROWTYPE;
  c agent.dev_cycles%ROWTYPE; a agent.tool_attempts%ROWTYPE; i agent.tool_intents%ROWTYPE;
  approval agent.approval_records%ROWTYPE; effect agent.side_effect_records%ROWTYPE; incarnation bigint;
BEGIN
  PERFORM agent.execution_lock_scope(p_tenant,p_owner,p_workspace);
  SELECT * INTO t FROM agent.tasks WHERE tenant_id=p_tenant AND id=p_task FOR UPDATE;
  SELECT * INTO r FROM agent.runs WHERE tenant_id=p_tenant AND id=p_run AND task_id=p_task FOR UPDATE;
  SELECT restore_incarnation INTO incarnation FROM agent.schema_control WHERE singleton;
  IF t.id IS NULL OR r.id IS NULL OR incarnation IS NULL OR t.owner_user_id<>p_owner
     OR t.workspace_id<>p_workspace OR t.active_run_id<>p_run OR t.goal_revision<>p_goal
     OR r.lease_epoch<>p_lease OR r.lease_owner_ref IS DISTINCT FROM p_worker
     OR r.lease_expires_at IS NULL OR r.lease_expires_at<=clock_timestamp()
     OR r.restore_incarnation<>p_restore OR incarnation<>p_restore OR r.cancel_requested OR r.pause_requested
     OR r.status_code NOT IN ('CLAIMED','RUNNING')
  THEN RAISE EXCEPTION 'STALE_GIT_OPERATION' USING ERRCODE='insufficient_privilege'; END IF;
  SELECT * INTO hint FROM agent.git_operations WHERE tenant_id=p_tenant AND id=p_operation;
  IF hint.id IS NULL THEN RAISE EXCEPTION 'GIT_OPERATION_UNAVAILABLE' USING ERRCODE='insufficient_privilege'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('execution-workspace:' || p_tenant::text || ':' || p_workspace::text, 0));
  SELECT * INTO b FROM agent.repository_bindings WHERE tenant_id=p_tenant AND id=hint.binding_id FOR UPDATE;
  SELECT * INTO p FROM agent.git_patch_artifacts WHERE tenant_id=p_tenant AND id=hint.patch_id FOR UPDATE;
  SELECT * INTO c FROM agent.dev_cycles WHERE tenant_id=p_tenant AND id=hint.cycle_id FOR UPDATE;
  SELECT * INTO a FROM agent.tool_attempts WHERE tenant_id=p_tenant AND id=hint.attempt_id FOR UPDATE;
  SELECT * INTO i FROM agent.tool_intents WHERE tenant_id=p_tenant AND id=hint.intent_id FOR UPDATE;
  SELECT * INTO approval FROM agent.approval_records WHERE tenant_id=p_tenant AND tool_intent_id=i.id FOR SHARE;
  SELECT * INTO effect FROM agent.side_effect_records WHERE tenant_id=p_tenant AND tool_intent_id=i.id FOR UPDATE;
  SELECT * INTO o FROM agent.git_operations WHERE tenant_id=p_tenant AND id=p_operation FOR UPDATE;
  IF o.id IS NULL OR b.id IS NULL OR NOT b.active OR b.owner_user_id<>p_owner OR b.workspace_id<>p_workspace
     OR b.generation<>o.binding_generation OR b.approved_branch IS DISTINCT FROM o.branch
     OR b.approved_commit<>o.parent_sha OR p.id IS NULL OR NOT p.active OR p.owner_user_id<>p_owner
     OR p.workspace_id<>p_workspace OR p.binding_id<>b.id OR p.binding_generation<>b.generation
     OR p.parent_sha<>o.parent_sha OR p.diff_sha256<>o.patch_sha256 OR p.task_id<>p_task OR p.run_id<>p_run
     OR c.id IS NULL OR c.owner_user_id<>p_owner OR c.workspace_id<>p_workspace OR c.task_id<>p_task OR c.run_id<>p_run
     OR c.phase NOT IN ('PATCH_READY','COMMIT_PENDING') OR c.cycle_json->'patch'->>'artifact_ref' IS DISTINCT FROM p.id::text
     OR c.cycle_json->'patch'->>'parent_sha' IS DISTINCT FROM p.parent_sha
     OR c.cycle_json->'patch'->>'diff_sha256' IS DISTINCT FROM p.diff_sha256
     OR c.cycle_json->'patch'->'paths' IS DISTINCT FROM to_jsonb(p.paths)
     OR c.cycle_json->'plan'->>'plan_ref' IS DISTINCT FROM o.plan_ref
     OR c.cycle_json->'plan'->>'goal_revision' IS DISTINCT FROM p_goal::text
     OR c.cycle_json->'run'->>'task_id' IS DISTINCT FROM p_task::text
     OR c.cycle_json->'run'->>'run_id' IS DISTINCT FROM p_run::text
     OR c.cycle_json->'run'->>'goal_revision' IS DISTINCT FROM p_goal::text
     OR c.cycle_json->'run'->>'lease_epoch' IS DISTINCT FROM p_lease::text
     OR c.cycle_json->'run'->>'restore_incarnation' IS DISTINCT FROM p_restore::text
     OR c.cycle_json->'run'->>'worker_ref' IS DISTINCT FROM p_worker
     OR a.id IS NULL OR a.tool_intent_id<>o.intent_id OR a.run_lease_epoch<>p_lease OR a.worker_ref<>p_worker
     OR a.status_code NOT IN ('SENDING','CONFIRMED') OR i.id IS NULL OR i.tool_key<>'git.commit'
     OR i.task_id<>p_task OR i.run_id<>p_run OR i.goal_revision<>p_goal
     OR i.status_code NOT IN ('SENDING','CONFIRMED') OR i.request_sha256<>o.request_sha256
     OR approval.id IS NULL OR approval.decision_code<>'APPROVED' OR approval.expires_at<=clock_timestamp()
     OR approval.owner_user_id<>p_owner OR approval.task_id<>p_task OR approval.run_id<>p_run
     OR approval.goal_revision<>p_goal OR approval.tool_key<>i.tool_key OR approval.request_sha256<>i.request_sha256
     OR effect.id IS NULL OR effect.effect_key<>o.effect_key OR effect.request_sha256<>i.request_sha256
     OR effect.attempt_fence<>a.fence_token OR effect.outcome_code NOT IN ('SENDING','CONFIRMED')
  THEN RAISE EXCEPTION 'GIT_RECEIPT_SCOPE_STALE' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN o;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_receipt_lock(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;

CREATE FUNCTION agent.git_operation_finish(
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
  IF o.state<>'ADMITTED' OR NOT EXISTS (
    SELECT 1 FROM agent.tool_attempts a JOIN agent.tool_intents i
      ON i.tenant_id=a.tenant_id AND i.id=a.tool_intent_id WHERE a.tenant_id=p_tenant AND a.id=o.attempt_id
      AND a.status_code='SENDING' AND i.status_code='SENDING'
  ) THEN RAISE EXCEPTION 'GIT_OPERATION_NOT_ADMITTED' USING ERRCODE='check_violation'; END IF;
  INSERT INTO agent.git_operation_receipts(tenant_id,operation_id,receipt_id,body,proof,body_sha256,proof_sha256)
    VALUES(p_tenant,o.id,p_receipt,p_body,p_proof,p_body_sha,p_proof_sha);
  UPDATE agent.git_operations SET state='RECEIPT_PERSISTED' WHERE tenant_id=p_tenant AND id=o.id;
  RETURN p_receipt;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_finish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_finish(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,bytea,bytea,text,text) TO aiagent_tool_gateway;

CREATE FUNCTION agent.git_operation_fetch(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid
) RETURNS TABLE(receipt_id uuid,body bytea,proof bytea,body_sha256 text,proof_sha256 text)
LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE o agent.git_operations%ROWTYPE;
BEGIN
  o := agent.git_operation_receipt_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_operation);
  IF o.state<>'RECEIPT_PERSISTED' THEN RAISE EXCEPTION 'GIT_RECEIPT_UNAVAILABLE' USING ERRCODE='insufficient_privilege'; END IF;
  RETURN QUERY SELECT r.receipt_id,r.body,r.proof,r.body_sha256,r.proof_sha256
    FROM agent.git_operation_receipts r WHERE r.tenant_id=p_tenant AND r.operation_id=o.id;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_fetch(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_fetch(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid) TO aiagent_tool_gateway;
