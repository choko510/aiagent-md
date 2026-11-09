-- Roles are cluster-wide and may be shared by other databases: they are not dropped, only their grants here.
DROP FUNCTION agent.gc_terminal_runs(interval);
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_policies WHERE schemaname = 'agent' GROUP BY tablename LOOP
    EXECUTE format('DROP POLICY IF EXISTS api_tenant ON agent.%I', t);
    EXECUTE format('DROP POLICY IF EXISTS service_all ON agent.%I', t);
    EXECUTE format('ALTER TABLE agent.%I DISABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA agent, queue, lg_checkpoint FROM aiagent_api, aiagent_runtime, aiagent_checkpointer,
  aiagent_model_gateway, aiagent_tool_gateway, aiagent_relay, aiagent_sentinel, aiagent_ops;
REVOKE ALL ON SCHEMA agent, queue, lg_checkpoint FROM aiagent_api, aiagent_runtime, aiagent_checkpointer,
  aiagent_model_gateway, aiagent_tool_gateway, aiagent_relay, aiagent_sentinel, aiagent_ops;
REVOKE ALL ON FUNCTION lg_checkpoint.gc(interval) FROM aiagent_ops;
