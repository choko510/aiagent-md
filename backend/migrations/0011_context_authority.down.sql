DROP FUNCTION IF EXISTS agent.gc_orphan_context_artifacts(interval);
DROP FUNCTION IF EXISTS agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text);
DROP FUNCTION IF EXISTS agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid);
DROP FUNCTION IF EXISTS agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint);
DROP FUNCTION IF EXISTS agent.memory_artifact_ok(uuid, uuid);
DROP FUNCTION IF EXISTS agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint);
DROP FUNCTION IF EXISTS agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.memory_restore_ready(uuid, uuid, uuid);
DROP TABLE IF EXISTS agent.memory_restore_state;
DROP FUNCTION IF EXISTS agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid);
DROP FUNCTION IF EXISTS agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text);
DROP FUNCTION IF EXISTS agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text);
DROP FUNCTION IF EXISTS agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea,
  bytea, timestamptz, text);
DROP FUNCTION IF EXISTS agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer,
  bytea, bytea, timestamptz, text);
DROP FUNCTION IF EXISTS agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text);
DROP FUNCTION IF EXISTS agent.scope_active(uuid, uuid, uuid);
GRANT EXECUTE ON FUNCTION agent.memory_artifact_usable(uuid, uuid, uuid, uuid) TO aiagent_model_gateway;
GRANT EXECUTE ON FUNCTION agent.lock_memory_epoch(uuid, uuid, uuid, bigint) TO aiagent_model_gateway;
GRANT EXECUTE ON FUNCTION agent.revoke_context_artifact(uuid, uuid, uuid, uuid, uuid) TO aiagent_model_gateway;
ALTER TABLE agent.context_artifacts DROP COLUMN key_id;
-- Restore the pre-0011 posture so older down scripts still apply.
GRANT SELECT, INSERT ON agent.context_artifacts TO aiagent_model_gateway;
GRANT UPDATE (revoked_at, ciphertext) ON agent.context_artifacts TO aiagent_model_gateway;
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
GRANT SELECT ON agent.memory_scope_epochs, agent.memory_records TO aiagent_model_gateway;
GRANT SELECT, INSERT ON agent.context_dispatches TO aiagent_model_gateway;
GRANT UPDATE (status_code) ON agent.context_dispatches TO aiagent_model_gateway;
DROP POLICY IF EXISTS memory_epoch_scope ON agent.memory_scope_epochs;
CREATE POLICY memory_epoch_scope ON agent.memory_scope_epochs TO aiagent_api, aiagent_model_gateway
 USING (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true))
 WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true));
DROP POLICY IF EXISTS memory_record_scope ON agent.memory_records;
CREATE POLICY memory_record_scope ON agent.memory_records TO aiagent_api, aiagent_model_gateway
 USING (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true))
 WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true));
CREATE POLICY context_dispatch_scope ON agent.context_dispatches TO aiagent_model_gateway
 USING(tenant_id::text=current_setting('aiagent.memory_tenant',true)
   AND owner_user_id::text=current_setting('aiagent.memory_owner',true)
   AND task_id::text=current_setting('aiagent.memory_task',true))
 WITH CHECK(tenant_id::text=current_setting('aiagent.memory_tenant',true)
   AND owner_user_id::text=current_setting('aiagent.memory_owner',true)
   AND task_id::text=current_setting('aiagent.memory_task',true));
