-- Defense in depth for Context artifact queries. Session scope is set only from
-- an already validated server-side Run claim. This does not authenticate SQL clients.
DROP POLICY context_artifacts_model_gateway ON agent.context_artifacts;
CREATE POLICY context_artifacts_read_scope ON agent.context_artifacts
  FOR SELECT TO aiagent_model_gateway
  USING (tenant_id::text = current_setting('aiagent.artifact_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.artifact_owner', true)
     AND task_id::text = current_setting('aiagent.artifact_task', true));
CREATE POLICY context_artifacts_insert_scope ON agent.context_artifacts
  FOR INSERT TO aiagent_model_gateway
  WITH CHECK (tenant_id::text = current_setting('aiagent.artifact_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.artifact_owner', true)
     AND task_id::text = current_setting('aiagent.artifact_task', true)
     AND run_id::text = current_setting('aiagent.artifact_run', true));
CREATE POLICY context_artifacts_update_scope ON agent.context_artifacts
  FOR UPDATE TO aiagent_model_gateway
  USING (tenant_id::text = current_setting('aiagent.artifact_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.artifact_owner', true)
     AND task_id::text = current_setting('aiagent.artifact_task', true)
     AND run_id::text = current_setting('aiagent.artifact_run', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.artifact_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.artifact_owner', true)
     AND task_id::text = current_setting('aiagent.artifact_task', true)
     AND run_id::text = current_setting('aiagent.artifact_run', true));
