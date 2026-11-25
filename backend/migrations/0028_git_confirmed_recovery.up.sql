-- Read an already-confirmed Git receipt for its persisted Workflow intent.
CREATE OR REPLACE FUNCTION agent.git_operation_receipt_lock(
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
     OR c.phase NOT IN ('PATCH_READY','COMMIT_PENDING','COMMITTED','UNKNOWN_EXTERNAL_EFFECT')
     OR (c.phase='UNKNOWN_EXTERNAL_EFFECT' AND (
       o.state<>'RECEIPT_PERSISTED' OR i.status_code<>'CONFIRMED' OR a.status_code<>'CONFIRMED'
       OR effect.outcome_code<>'CONFIRMED' OR c.cycle_json->>'commit_intent_ref' IS DISTINCT FROM i.invocation_key
       OR NOT EXISTS (SELECT 1 FROM agent.git_operation_receipts gr
         WHERE gr.tenant_id=p_tenant AND gr.operation_id=o.id
           AND effect.external_receipt_ref='git:' || gr.receipt_id::text)
     ))
     OR (c.phase='COMMITTED' AND NOT EXISTS (
       SELECT 1 FROM agent.git_operation_receipts gr WHERE gr.tenant_id=p_tenant AND gr.operation_id=o.id
         AND c.cycle_json->'commit'->>'receipt_ref'='git:' || gr.receipt_id::text
         AND c.cycle_json->'commit'->>'commit_sha'=convert_from(gr.body,'UTF8')::jsonb->>'commit'
         AND c.cycle_json->'commit'->>'parent_sha'=o.parent_sha
         AND c.cycle_json->'commit'->>'artifact_ref'=o.patch_id::text
         AND c.cycle_json->'commit'->>'diff_sha256'=o.patch_sha256
     ))
     OR c.cycle_json->'patch'->>'artifact_ref' IS DISTINCT FROM p.id::text
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

