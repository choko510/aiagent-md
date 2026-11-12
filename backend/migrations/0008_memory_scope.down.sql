DROP TRIGGER memory_record_guard ON agent.memory_records;
DROP FUNCTION agent.guard_memory_record();
DROP TRIGGER memory_epoch_guard ON agent.memory_scope_epochs;
DROP FUNCTION agent.guard_memory_epoch();
DROP FUNCTION agent.revoke_context_artifact(uuid,uuid,uuid,uuid,uuid);
GRANT UPDATE (revoked_at,ciphertext) ON agent.context_artifacts TO aiagent_model_gateway;
CREATE OR REPLACE FUNCTION agent.gc_expired_context_artifacts() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
 UPDATE agent.context_artifacts SET revoked_at=clock_timestamp(), ciphertext=NULL
  WHERE revoked_at IS NULL AND expires_at <= clock_timestamp();
 GET DIAGNOSTICS n = ROW_COUNT;
 RETURN n;
END $$;
DROP FUNCTION agent.lock_memory_epoch(uuid,uuid,uuid,bigint);
DROP FUNCTION agent.memory_artifact_usable(uuid,uuid,uuid,uuid);
DROP TABLE agent.memory_records;
DROP TABLE agent.memory_scope_epochs;
