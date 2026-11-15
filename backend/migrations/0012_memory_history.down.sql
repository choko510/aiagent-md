-- Restore the exact 0008 guard (revocation-only updates).
CREATE OR REPLACE FUNCTION agent.guard_memory_record() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  PERFORM 1 FROM agent.memory_scope_epochs e WHERE e.tenant_id = NEW.tenant_id
    AND e.owner_user_id = NEW.owner_user_id AND e.task_id = NEW.task_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'memory scope missing' USING ERRCODE = 'check_violation'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.revision <> 1 OR NEW.revoked_at IS NOT NULL OR NOT EXISTS
      (SELECT 1 FROM agent.tasks t WHERE t.tenant_id = NEW.tenant_id AND t.id = NEW.task_id
        AND t.owner_user_id = NEW.owner_user_id) THEN
      RAISE EXCEPTION 'invalid memory owner' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM agent.context_artifacts a WHERE a.tenant_id = NEW.tenant_id
      AND a.owner_user_id = NEW.owner_user_id AND a.task_id = NEW.task_id
      AND a.id IN (NEW.source_artifact_id, NEW.projection_artifact_id)
      AND a.kind = 'raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp() FOR SHARE;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> (CASE WHEN NEW.source_artifact_id = NEW.projection_artifact_id THEN 1 ELSE 2 END) THEN
      RAISE EXCEPTION 'invalid memory source' USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revision <> OLD.revision + 1 OR
      (NEW.tenant_id, NEW.owner_user_id, NEW.task_id, NEW.id, NEW.source_artifact_id, NEW.projection_artifact_id,
       NEW.recipient, NEW.consent_basis, NEW.observed_at, NEW.expires_at, NEW.created_at) IS DISTINCT FROM
      (OLD.tenant_id, OLD.owner_user_id, OLD.task_id, OLD.id, OLD.source_artifact_id, OLD.projection_artifact_id,
       OLD.recipient, OLD.consent_basis, OLD.observed_at, OLD.expires_at, OLD.created_at) THEN
      RAISE EXCEPTION 'invalid memory mutation' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1 WHERE tenant_id = NEW.tenant_id
    AND owner_user_id = NEW.owner_user_id AND task_id = NEW.task_id;
  RETURN NEW;
END $$;
ALTER TABLE agent.memory_records DROP COLUMN updated_at;
DROP TABLE IF EXISTS agent.memory_tombstone_watermarks;
DROP TRIGGER IF EXISTS memory_revision_append_only ON agent.memory_record_revisions;
DROP TABLE IF EXISTS agent.memory_record_revisions;
