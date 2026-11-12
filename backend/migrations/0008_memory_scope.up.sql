-- STEP 4 draft: task-scoped Memory revision barrier. The writer is the API role;
-- model gateway may read and acquire the barrier but cannot change memory or epochs.
CREATE TABLE agent.memory_scope_epochs (
 tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL, task_id uuid NOT NULL,
 source_epoch bigint NOT NULL DEFAULT 1 CHECK (source_epoch BETWEEN 1 AND 9223372036854775807),
 PRIMARY KEY (tenant_id, owner_user_id, task_id),
 FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id)
);
CREATE TABLE agent.memory_records (
 tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL, task_id uuid NOT NULL,
 id uuid NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
 source_artifact_id uuid NOT NULL, projection_artifact_id uuid NOT NULL,
 recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
 consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
 observed_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
 revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,id),
 FOREIGN KEY (tenant_id,owner_user_id,task_id) REFERENCES agent.memory_scope_epochs(tenant_id,owner_user_id,task_id),
 FOREIGN KEY (tenant_id,source_artifact_id) REFERENCES agent.context_artifacts(tenant_id,id),
 FOREIGN KEY (tenant_id,projection_artifact_id) REFERENCES agent.context_artifacts(tenant_id,id),
 CHECK(expires_at > created_at)
);
CREATE INDEX memory_records_scope ON agent.memory_records(tenant_id,owner_user_id,task_id) WHERE revoked_at IS NULL;
ALTER TABLE agent.memory_scope_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.memory_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_epoch_scope ON agent.memory_scope_epochs TO aiagent_api, aiagent_model_gateway
 USING (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true))
 WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true));
CREATE POLICY memory_record_scope ON agent.memory_records TO aiagent_api, aiagent_model_gateway
 USING (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true))
 WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant',true)
    AND owner_user_id::text = current_setting('aiagent.memory_owner',true)
    AND task_id::text = current_setting('aiagent.memory_task',true));
GRANT SELECT,INSERT,UPDATE ON agent.memory_scope_epochs,agent.memory_records TO aiagent_api;
GRANT SELECT ON agent.memory_scope_epochs,agent.memory_records TO aiagent_model_gateway;
-- This function is a metadata check only. It cannot read artifact ciphertext.
CREATE FUNCTION agent.memory_artifact_usable(p_tenant uuid,p_owner uuid,p_task uuid,p_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,agent AS $$
 SELECT p_tenant::text = current_setting('aiagent.memory_tenant',true)
    AND p_owner::text = current_setting('aiagent.memory_owner',true)
    AND p_task::text = current_setting('aiagent.memory_task',true)
    AND EXISTS (SELECT 1 FROM agent.context_artifacts a
      WHERE a.tenant_id=p_tenant AND a.owner_user_id=p_owner AND a.task_id=p_task AND a.id=p_id
        AND a.kind='raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp())
$$;
REVOKE ALL ON FUNCTION agent.memory_artifact_usable(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_artifact_usable(uuid,uuid,uuid,uuid) TO aiagent_api,aiagent_model_gateway;
-- Row lock remains held until caller commits, so an admitted snapshot and a writer
-- have a deterministic order. This is not an external-send transaction.
CREATE FUNCTION agent.lock_memory_epoch(p_tenant uuid,p_owner uuid,p_task uuid,p_epoch bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,agent AS $$
DECLARE actual bigint;
BEGIN
 IF p_tenant::text IS DISTINCT FROM current_setting('aiagent.memory_tenant',true)
 OR p_owner::text IS DISTINCT FROM current_setting('aiagent.memory_owner',true)
 OR p_task::text IS DISTINCT FROM current_setting('aiagent.memory_task',true) THEN RETURN false; END IF;
 SELECT source_epoch INTO actual FROM agent.memory_scope_epochs
  WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task FOR UPDATE;
 RETURN coalesce(actual=p_epoch,false);
END $$;
REVOKE ALL ON FUNCTION agent.lock_memory_epoch(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.lock_memory_epoch(uuid,uuid,uuid,bigint) TO aiagent_model_gateway;
-- Revoke source and advance its task epoch under the same row lock and transaction.
-- The API writer and model gateway never share write privileges over Memory rows.
REVOKE UPDATE (revoked_at,ciphertext) ON agent.context_artifacts FROM aiagent_model_gateway;
CREATE FUNCTION agent.revoke_context_artifact(p_tenant uuid,p_owner uuid,p_task uuid,p_run uuid,p_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,agent AS $$
DECLARE n integer;
BEGIN
 IF p_tenant::text IS DISTINCT FROM current_setting('aiagent.artifact_tenant',true)
 OR p_owner::text IS DISTINCT FROM current_setting('aiagent.artifact_owner',true)
 OR p_task::text IS DISTINCT FROM current_setting('aiagent.artifact_task',true)
 OR p_run::text IS DISTINCT FROM current_setting('aiagent.artifact_run',true) THEN RETURN false; END IF;
 INSERT INTO agent.memory_scope_epochs(tenant_id,owner_user_id,task_id) VALUES(p_tenant,p_owner,p_task)
  ON CONFLICT DO NOTHING;
 PERFORM 1 FROM agent.memory_scope_epochs WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task FOR UPDATE;
 UPDATE agent.context_artifacts SET revoked_at=clock_timestamp(),ciphertext=NULL
  WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task AND id=p_id AND revoked_at IS NULL;
 GET DIAGNOSTICS n = ROW_COUNT;
 IF n > 0 THEN
  UPDATE agent.memory_scope_epochs SET source_epoch=source_epoch+1
   WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task;
 END IF;
 RETURN n > 0;
END $$;
REVOKE ALL ON FUNCTION agent.revoke_context_artifact(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.revoke_context_artifact(uuid,uuid,uuid,uuid,uuid) TO aiagent_model_gateway;
CREATE OR REPLACE FUNCTION agent.gc_expired_context_artifacts() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,agent AS $$
DECLARE r record; n integer; total integer := 0;
BEGIN
 FOR r IN SELECT DISTINCT tenant_id,owner_user_id,task_id FROM agent.context_artifacts
   WHERE revoked_at IS NULL AND expires_at <= clock_timestamp() ORDER BY tenant_id,owner_user_id,task_id LOOP
  INSERT INTO agent.memory_scope_epochs(tenant_id,owner_user_id,task_id) VALUES(r.tenant_id,r.owner_user_id,r.task_id)
   ON CONFLICT DO NOTHING;
  PERFORM 1 FROM agent.memory_scope_epochs WHERE tenant_id=r.tenant_id AND owner_user_id=r.owner_user_id
   AND task_id=r.task_id FOR UPDATE;
  UPDATE agent.context_artifacts SET revoked_at=clock_timestamp(),ciphertext=NULL
   WHERE tenant_id=r.tenant_id AND owner_user_id=r.owner_user_id AND task_id=r.task_id
    AND revoked_at IS NULL AND expires_at <= clock_timestamp();
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
   UPDATE agent.memory_scope_epochs SET source_epoch=source_epoch+1
    WHERE tenant_id=r.tenant_id AND owner_user_id=r.owner_user_id AND task_id=r.task_id;
   total := total+n;
  END IF;
 END LOOP;
 RETURN total;
END $$;
CREATE FUNCTION agent.guard_memory_epoch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (TG_OP='INSERT' AND NEW.source_epoch <> 1) OR (TG_OP='UPDATE' AND
   (NEW.source_epoch <> OLD.source_epoch+1 OR (NEW.tenant_id,NEW.owner_user_id,NEW.task_id)
    IS DISTINCT FROM (OLD.tenant_id,OLD.owner_user_id,OLD.task_id))) THEN
  RAISE EXCEPTION 'invalid memory epoch transition' USING ERRCODE='check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER memory_epoch_guard BEFORE INSERT OR UPDATE ON agent.memory_scope_epochs
 FOR EACH ROW EXECUTE FUNCTION agent.guard_memory_epoch();
CREATE FUNCTION agent.guard_memory_record() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,agent AS $$
DECLARE n integer;
BEGIN
 PERFORM 1 FROM agent.memory_scope_epochs e WHERE e.tenant_id=NEW.tenant_id
  AND e.owner_user_id=NEW.owner_user_id AND e.task_id=NEW.task_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'memory scope missing' USING ERRCODE='check_violation'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.revision <> 1 OR NEW.revoked_at IS NOT NULL OR NOT EXISTS
   (SELECT 1 FROM agent.tasks t WHERE t.tenant_id=NEW.tenant_id AND t.id=NEW.task_id
    AND t.owner_user_id=NEW.owner_user_id) THEN
   RAISE EXCEPTION 'invalid memory owner' USING ERRCODE='check_violation'; END IF;
  PERFORM 1 FROM agent.context_artifacts a WHERE a.tenant_id=NEW.tenant_id
   AND a.owner_user_id=NEW.owner_user_id AND a.task_id=NEW.task_id
   AND a.id IN (NEW.source_artifact_id,NEW.projection_artifact_id)
   AND a.kind='raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp() FOR SHARE;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> (CASE WHEN NEW.source_artifact_id=NEW.projection_artifact_id THEN 1 ELSE 2 END) THEN
   RAISE EXCEPTION 'invalid memory source' USING ERRCODE='check_violation'; END IF;
 ELSE
  IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revision <> OLD.revision+1 OR
    (NEW.tenant_id,NEW.owner_user_id,NEW.task_id,NEW.id,NEW.source_artifact_id,NEW.projection_artifact_id,
     NEW.recipient,NEW.consent_basis,NEW.observed_at,NEW.expires_at,NEW.created_at) IS DISTINCT FROM
    (OLD.tenant_id,OLD.owner_user_id,OLD.task_id,OLD.id,OLD.source_artifact_id,OLD.projection_artifact_id,
     OLD.recipient,OLD.consent_basis,OLD.observed_at,OLD.expires_at,OLD.created_at) THEN
   RAISE EXCEPTION 'invalid memory mutation' USING ERRCODE='check_violation'; END IF;
 END IF;
 UPDATE agent.memory_scope_epochs SET source_epoch=source_epoch+1 WHERE tenant_id=NEW.tenant_id
  AND owner_user_id=NEW.owner_user_id AND task_id=NEW.task_id;
 RETURN NEW;
END $$;
CREATE TRIGGER memory_record_guard BEFORE INSERT OR UPDATE ON agent.memory_records
 FOR EACH ROW EXECUTE FUNCTION agent.guard_memory_record();
