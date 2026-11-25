-- Permit only receipt-bound COMMITTED cycles to revalidate before snapshot promotion.
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
     OR c.phase NOT IN ('PATCH_READY','COMMIT_PENDING','COMMITTED')
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

-- Gateway must first authenticate the stored receipt and independently stage
-- the full new Git projection. This transaction fences all authorities again.
CREATE FUNCTION agent.git_promote_verified_snapshot(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint, p_operation uuid,
  p_receipt uuid, p_body_sha text, p_snapshot uuid, p_tree text,
  p_manifest_sha text, p_source_sha text, p_source_bytes bigint, p_strength text
) RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE o agent.git_operations%ROWTYPE; b agent.repository_bindings%ROWTYPE;
        gr agent.git_operation_receipts%ROWTYPE; c agent.dev_cycles%ROWTYPE;
        prior_snapshot agent.workspace_snapshot_attestations%ROWTYPE; payload jsonb;
BEGIN
  IF p_body_sha !~ '^[0-9a-f]{64}$' OR p_manifest_sha !~ '^[0-9a-f]{64}$'
     OR p_source_sha !~ '^[0-9a-f]{64}$' OR p_tree !~ '^([0-9a-f]{40}|[0-9a-f]{64})$'
     OR p_source_bytes NOT BETWEEN 1 AND 134217728 OR p_strength NOT IN ('LEGACY_SHA1','SHA256')
  THEN RAISE EXCEPTION 'INVALID_GIT_SNAPSHOT' USING ERRCODE='check_violation'; END IF;
  o := agent.git_operation_receipt_lock(p_tenant,p_owner,p_workspace,p_task,p_run,p_goal,p_lease,p_worker,p_restore,p_operation);
  SELECT * INTO b FROM agent.repository_bindings WHERE tenant_id=p_tenant AND id=o.binding_id;
  SELECT * INTO c FROM agent.dev_cycles WHERE tenant_id=p_tenant AND id=o.cycle_id;
  SELECT * INTO gr FROM agent.git_operation_receipts WHERE tenant_id=p_tenant AND operation_id=o.id;
  SELECT s.* INTO prior_snapshot FROM agent.workspace_snapshot_heads h
    JOIN agent.workspace_snapshot_attestations s ON s.tenant_id=h.tenant_id AND s.id=h.snapshot_id
    WHERE h.tenant_id=p_tenant AND h.workspace_id=p_workspace FOR UPDATE OF h;
  IF gr.receipt_id IS NULL OR gr.receipt_id<>p_receipt OR gr.body_sha256<>p_body_sha THEN
    RAISE EXCEPTION 'GIT_SNAPSHOT_RECEIPT_MISMATCH' USING ERRCODE='insufficient_privilege';
  END IF;
  payload := convert_from(gr.body,'UTF8')::jsonb;
  IF o.state<>'RECEIPT_PERSISTED' OR c.phase<>'COMMITTED' OR prior_snapshot.id IS NULL
     OR prior_snapshot.owner_user_id<>p_owner OR prior_snapshot.binding_id<>b.id
     OR prior_snapshot.binding_generation<>b.generation OR prior_snapshot.workspace_revision<>b.workspace_revision
     OR prior_snapshot.commit_sha<>o.parent_sha OR o.candidate_commit IS NULL
     OR o.candidate_tree IS NULL OR o.prepared_journal_sha256 IS NULL
     OR payload->>'commit' IS DISTINCT FROM o.candidate_commit OR payload->>'tree' IS DISTINCT FROM o.candidate_tree
     OR p_tree IS DISTINCT FROM o.candidate_tree OR c.cycle_json->'commit'->>'commit_sha' IS DISTINCT FROM o.candidate_commit
     OR c.cycle_json->'commit'->>'receipt_ref' IS DISTINCT FROM 'git:' || p_receipt::text
     OR NOT EXISTS (SELECT 1 FROM agent.tool_intents i JOIN agent.tool_attempts a
       ON a.tenant_id=i.tenant_id AND a.tool_intent_id=i.id JOIN agent.side_effect_records e
       ON e.tenant_id=i.tenant_id AND e.tool_intent_id=i.id
       WHERE i.tenant_id=p_tenant AND i.id=o.intent_id AND a.id=o.attempt_id
         AND i.status_code='CONFIRMED' AND a.status_code='CONFIRMED' AND e.outcome_code='CONFIRMED'
         AND e.external_receipt_ref='git:' || p_receipt::text)
  THEN RAISE EXCEPTION 'GIT_SNAPSHOT_NOT_CONFIRMED' USING ERRCODE='insufficient_privilege'; END IF;
  UPDATE agent.repository_bindings SET approved_commit=o.candidate_commit, generation=generation+1,
    workspace_revision=workspace_revision+1 WHERE tenant_id=p_tenant AND id=b.id;
  INSERT INTO agent.workspace_snapshot_attestations(tenant_id,id,owner_user_id,workspace_id,binding_id,
    binding_generation,workspace_revision,commit_sha,tree_sha,manifest_sha256,source_sha256,source_bytes,hash_strength)
  VALUES (p_tenant,p_snapshot,p_owner,p_workspace,b.id,b.generation+1,b.workspace_revision+1,o.candidate_commit,
    p_tree,p_manifest_sha,p_source_sha,p_source_bytes,p_strength);
  INSERT INTO agent.workspace_snapshot_heads(tenant_id,workspace_id,snapshot_id) VALUES (p_tenant,p_workspace,p_snapshot)
    ON CONFLICT (tenant_id,workspace_id) DO UPDATE SET snapshot_id=excluded.snapshot_id;
  RETURN p_snapshot;
END $$;
REVOKE ALL ON FUNCTION agent.git_promote_verified_snapshot(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,text,uuid,text,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_promote_verified_snapshot(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,text,uuid,text,text,text,bigint,text) TO aiagent_tool_gateway;
