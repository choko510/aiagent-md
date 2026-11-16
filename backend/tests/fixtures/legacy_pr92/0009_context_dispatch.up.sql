-- One committed admission per Context HEAD. An admitted request with no
-- completed result is UNKNOWN and is never automatically sent a second time.
CREATE TABLE agent.context_dispatches (
 tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL, task_id uuid NOT NULL, run_id uuid NOT NULL,
 head_revision bigint NOT NULL, model_attempt_id uuid NOT NULL,
 status_code text NOT NULL CHECK(status_code IN ('ADMITTED','COMPLETED','UNKNOWN')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,run_id,head_revision),
 FOREIGN KEY(tenant_id,task_id,run_id) REFERENCES agent.runs(tenant_id,task_id,id),
 FOREIGN KEY(tenant_id,run_id,head_revision) REFERENCES agent.run_checkpoints(tenant_id,run_id,checkpoint_revision),
 FOREIGN KEY(tenant_id,model_attempt_id) REFERENCES agent.model_attempts(tenant_id,id)
);
ALTER TABLE agent.context_dispatches ENABLE ROW LEVEL SECURITY;
CREATE POLICY context_dispatch_scope ON agent.context_dispatches TO aiagent_model_gateway
 USING(tenant_id::text=current_setting('aiagent.memory_tenant',true)
   AND owner_user_id::text=current_setting('aiagent.memory_owner',true)
   AND task_id::text=current_setting('aiagent.memory_task',true))
 WITH CHECK(tenant_id::text=current_setting('aiagent.memory_tenant',true)
   AND owner_user_id::text=current_setting('aiagent.memory_owner',true)
   AND task_id::text=current_setting('aiagent.memory_task',true));
GRANT SELECT,INSERT ON agent.context_dispatches TO aiagent_model_gateway;
GRANT UPDATE(status_code) ON agent.context_dispatches TO aiagent_model_gateway;
CREATE FUNCTION agent.guard_context_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status_code <> 'ADMITTED' OR NEW.status_code NOT IN ('COMPLETED','UNKNOWN') THEN
  RAISE EXCEPTION 'invalid context dispatch transition' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER context_dispatch_guard BEFORE UPDATE ON agent.context_dispatches
 FOR EACH ROW EXECUTE FUNCTION agent.guard_context_dispatch();
