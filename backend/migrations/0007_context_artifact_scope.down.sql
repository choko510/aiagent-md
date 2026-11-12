DROP POLICY context_artifacts_read_scope ON agent.context_artifacts;
DROP POLICY context_artifacts_insert_scope ON agent.context_artifacts;
DROP POLICY context_artifacts_update_scope ON agent.context_artifacts;
CREATE POLICY context_artifacts_model_gateway ON agent.context_artifacts TO aiagent_model_gateway
  USING (true) WITH CHECK (true);
