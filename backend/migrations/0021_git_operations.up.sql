-- Durable, one-operation-per-effect fence. Direct insertion is disabled;
-- the narrow Gateway owner function validates canonical admission atomically.
CREATE TABLE agent.git_operations (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  intent_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  effect_key text NOT NULL CHECK (length(effect_key) BETWEEN 1 AND 200),
  binding_id uuid NOT NULL,
  binding_generation bigint NOT NULL CHECK (binding_generation >= 1),
  patch_id uuid NOT NULL,
  branch text NOT NULL CHECK (length(branch) BETWEEN 1 AND 200),
  parent_sha text NOT NULL CHECK (parent_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  patch_sha256 text NOT NULL CHECK (patch_sha256 ~ '^[0-9a-f]{64}$'),
  cycle_id text NOT NULL CHECK (length(cycle_id) BETWEEN 1 AND 200),
  plan_ref text NOT NULL CHECK (length(plan_ref) BETWEEN 1 AND 256),
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  state text NOT NULL DEFAULT 'ADMITTED' CHECK (state IN ('ADMITTED', 'UNKNOWN')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, effect_key),
  UNIQUE (tenant_id, intent_id),
  UNIQUE (tenant_id, attempt_id),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES agent.tool_intents(tenant_id, id),
  FOREIGN KEY (tenant_id, attempt_id) REFERENCES agent.tool_attempts(tenant_id, id),
  FOREIGN KEY (tenant_id, binding_id) REFERENCES agent.repository_bindings(tenant_id, id),
  FOREIGN KEY (tenant_id, patch_id) REFERENCES agent.git_patch_artifacts(tenant_id, id)
);
ALTER TABLE agent.git_operations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.git_operations FROM PUBLIC;
CREATE POLICY git_operation_gateway_read ON agent.git_operations FOR SELECT TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
GRANT SELECT ON agent.git_operations TO aiagent_tool_gateway;

-- This function reserves an operation only; it never authorizes a worker or
-- executes Git. Re-delivery must reconcile, never reissue the side effect.
CREATE FUNCTION agent.git_operation_admit(
  p_tenant uuid, p_owner uuid, p_workspace uuid, p_task uuid, p_run uuid,
  p_goal bigint, p_lease bigint, p_worker text, p_restore bigint,
  p_attempt uuid, p_binding uuid, p_patch uuid, p_operation uuid
) RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  t agent.tasks%ROWTYPE;
  r agent.runs%ROWTYPE;
  b agent.repository_bindings%ROWTYPE;
  p agent.git_patch_artifacts%ROWTYPE;
  c agent.dev_cycles%ROWTYPE;
  a agent.tool_attempts%ROWTYPE;
  i agent.tool_intents%ROWTYPE;
  approval agent.approval_records%ROWTYPE;
  effect agent.side_effect_records%ROWTYPE;
  incarnation bigint;
BEGIN
  PERFORM agent.execution_lock_scope(p_tenant, p_owner, p_workspace);
  SELECT * INTO t FROM agent.tasks WHERE tenant_id=p_tenant AND id=p_task FOR UPDATE;
  SELECT * INTO r FROM agent.runs WHERE tenant_id=p_tenant AND id=p_run AND task_id=p_task FOR UPDATE;
  SELECT restore_incarnation INTO incarnation FROM agent.schema_control WHERE singleton;
  IF t.id IS NULL OR r.id IS NULL OR incarnation IS NULL OR t.owner_user_id<>p_owner
     OR t.workspace_id<>p_workspace OR t.active_run_id<>p_run OR t.goal_revision<>p_goal
     OR r.lease_epoch<>p_lease OR r.lease_owner_ref IS DISTINCT FROM p_worker
     OR r.lease_expires_at IS NULL OR r.lease_expires_at<=clock_timestamp()
     OR r.restore_incarnation<>p_restore OR incarnation<>p_restore
     OR r.cancel_requested OR r.pause_requested OR r.status_code NOT IN ('CLAIMED','RUNNING')
  THEN RAISE EXCEPTION 'STALE_GIT_OPERATION' USING ERRCODE='insufficient_privilege'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('execution-workspace:' || p_tenant::text || ':' || p_workspace::text, 0));
  SELECT * INTO b FROM agent.repository_bindings WHERE tenant_id=p_tenant AND id=p_binding FOR UPDATE;
  SELECT * INTO p FROM agent.git_patch_artifacts WHERE tenant_id=p_tenant AND id=p_patch FOR UPDATE;
  SELECT * INTO c FROM agent.dev_cycles WHERE tenant_id=p_tenant AND id=p.cycle_id FOR UPDATE;
  SELECT * INTO a FROM agent.tool_attempts WHERE tenant_id=p_tenant AND id=p_attempt FOR UPDATE;
  SELECT * INTO i FROM agent.tool_intents WHERE tenant_id=p_tenant AND id=a.tool_intent_id FOR UPDATE;
  SELECT * INTO approval FROM agent.approval_records WHERE tenant_id=p_tenant AND tool_intent_id=i.id FOR SHARE;
  SELECT * INTO effect FROM agent.side_effect_records WHERE tenant_id=p_tenant AND tool_intent_id=i.id FOR UPDATE;
  IF b.id IS NULL OR NOT b.active OR b.owner_user_id<>p_owner OR b.workspace_id<>p_workspace
     OR b.approved_branch IS NULL OR p.id IS NULL OR NOT p.active OR p.owner_user_id<>p_owner
     OR p.workspace_id<>p_workspace OR p.binding_id<>b.id OR p.binding_generation<>b.generation
     OR p.parent_sha<>b.approved_commit OR p.task_id<>p_task OR p.run_id<>p_run
     OR c.id IS NULL OR c.owner_user_id<>p_owner OR c.workspace_id<>p_workspace
     OR c.task_id<>p_task OR c.run_id<>p_run OR c.phase NOT IN ('PATCH_READY','COMMIT_PENDING')
     OR c.cycle_json->'patch'->>'artifact_ref' IS DISTINCT FROM p.id::text
     OR c.cycle_json->'patch'->>'parent_sha' IS DISTINCT FROM p.parent_sha
     OR c.cycle_json->'patch'->>'diff_sha256' IS DISTINCT FROM p.diff_sha256
     OR c.cycle_json->'patch'->'paths' IS DISTINCT FROM to_jsonb(p.paths)
     OR c.cycle_json->'plan'->>'plan_ref' IS NULL
     OR c.cycle_json->'plan'->>'goal_revision' IS DISTINCT FROM p_goal::text
     OR c.cycle_json->'run'->>'task_id' IS DISTINCT FROM p_task::text
     OR c.cycle_json->'run'->>'run_id' IS DISTINCT FROM p_run::text
     OR c.cycle_json->'run'->>'goal_revision' IS DISTINCT FROM p_goal::text
     OR c.cycle_json->'run'->>'lease_epoch' IS DISTINCT FROM p_lease::text
     OR c.cycle_json->'run'->>'worker_ref' IS DISTINCT FROM p_worker
     OR c.cycle_json->'run'->>'restore_incarnation' IS DISTINCT FROM p_restore::text
     OR a.id IS NULL OR a.status_code<>'SENDING' OR a.run_lease_epoch<>p_lease OR a.worker_ref<>p_worker
     OR i.id IS NULL OR i.tool_key<>'git.commit' OR i.status_code<>'SENDING'
     OR i.task_id<>p_task OR i.run_id<>p_run OR i.goal_revision<>p_goal
     OR i.safe_arguments_json->>'bindingId' IS DISTINCT FROM b.id::text
     OR i.safe_arguments_json->>'bindingGeneration' IS DISTINCT FROM b.generation::text
     OR i.safe_arguments_json->>'patchId' IS DISTINCT FROM p.id::text
     OR i.safe_arguments_json->>'cycleId' IS DISTINCT FROM c.id
     OR i.safe_arguments_json->>'planRef' IS DISTINCT FROM c.cycle_json->'plan'->>'plan_ref'
     OR i.safe_arguments_json->>'branch' IS DISTINCT FROM b.approved_branch
     OR i.safe_arguments_json->>'parent' IS DISTINCT FROM p.parent_sha
     OR i.safe_arguments_json->>'patchSha256' IS DISTINCT FROM p.diff_sha256
     OR i.safe_arguments_json->'paths' IS DISTINCT FROM to_jsonb(p.paths)
     OR approval.id IS NULL OR approval.decision_code<>'APPROVED' OR approval.expires_at<=clock_timestamp()
     OR approval.owner_user_id<>p_owner OR approval.goal_revision<>p_goal
     OR approval.task_id<>p_task OR approval.run_id<>p_run
     OR approval.tool_key<>i.tool_key OR approval.request_sha256<>i.request_sha256
     OR effect.id IS NULL OR effect.outcome_code<>'SENDING' OR effect.attempt_fence<>a.fence_token
     OR effect.request_sha256<>i.request_sha256 OR effect.effect_key<>p_tenant::text || ':' || i.id::text
  THEN RAISE EXCEPTION 'GIT_OPERATION_DENIED' USING ERRCODE='insufficient_privilege'; END IF;
  INSERT INTO agent.git_operations(tenant_id,id,intent_id,attempt_id,effect_key,binding_id,binding_generation,
    patch_id,branch,parent_sha,patch_sha256,cycle_id,plan_ref,request_sha256)
  VALUES (p_tenant,p_operation,i.id,a.id,effect.effect_key,b.id,b.generation,p.id,b.approved_branch,
    p.parent_sha,p.diff_sha256,c.id,c.cycle_json->'plan'->>'plan_ref',i.request_sha256);
  RETURN p_operation;
END $$;
REVOKE ALL ON FUNCTION agent.git_operation_admit(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.git_operation_admit(uuid,uuid,uuid,uuid,uuid,bigint,bigint,text,bigint,uuid,uuid,uuid,uuid) TO aiagent_tool_gateway;
