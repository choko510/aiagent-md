-- 0012_memory_history: revision history, consent/audit trail, and external tombstone
-- high-watermark for restore reconciliation.
--
-- Memory text still lives only in encrypted artifacts; these rows carry metadata,
-- reviewer-visible history and the chain that proves a tombstone was recorded
-- outside the database (WAL/PITR cannot revert an external append-only log).

CREATE TABLE agent.memory_record_revisions (
  tenant_id uuid NOT NULL, owner_user_id uuid NOT NULL, task_id uuid NOT NULL, memory_id uuid NOT NULL,
  revision bigint NOT NULL CHECK (revision >= 1),
  change_kind text NOT NULL CHECK (change_kind IN ('created','updated','revoked')),
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  expires_at timestamptz NOT NULL,
  actor_user_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, memory_id, revision),
  FOREIGN KEY (tenant_id, memory_id) REFERENCES agent.memory_records(tenant_id, id)
);
CREATE INDEX memory_revision_task ON agent.memory_record_revisions(tenant_id, owner_user_id, task_id, recorded_at DESC);
ALTER TABLE agent.memory_record_revisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY memory_revision_scope ON agent.memory_record_revisions TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));
GRANT SELECT, INSERT ON agent.memory_record_revisions TO aiagent_api;
GRANT SELECT ON agent.memory_record_revisions TO aiagent_ops;
-- History rows are append-only for everyone except the retention operator flag.
CREATE TRIGGER memory_revision_append_only BEFORE UPDATE OR DELETE ON agent.memory_record_revisions
  FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();

-- External tombstone watermark: the operator's append-only log (outside PostgreSQL)
-- has a monotonically increasing generation. Restore reconciliation records the
-- generation it verified. Memory stays closed until the current incarnation has a row.
CREATE TABLE agent.memory_tombstone_watermarks (
  generation bigint PRIMARY KEY CHECK (generation >= 1),
  chain_head char(64) NOT NULL CHECK (chain_head ~ '^[0-9a-f]{64}$'),
  last_event_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON agent.memory_tombstone_watermarks TO aiagent_api, aiagent_ops;
GRANT INSERT ON agent.memory_tombstone_watermarks TO aiagent_ops;

-- Memory metadata updates (recipient / expiry) and revocation are two explicit
-- transitions of the existing guard; identity and content references never change.
ALTER TABLE agent.memory_records ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
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
  ELSIF OLD.revoked_at IS NOT NULL THEN
    RAISE EXCEPTION 'memory already revoked' USING ERRCODE = 'check_violation';
  ELSIF NEW.revision <> OLD.revision + 1
        AND NOT (current_setting('aiagent.memory_restore_reconcile', true) = 'on'
                 AND NEW.revision > OLD.revision) THEN
    RAISE EXCEPTION 'invalid memory revision' USING ERRCODE = 'check_violation';
  ELSIF (NEW.tenant_id, NEW.owner_user_id, NEW.task_id, NEW.id, NEW.source_artifact_id,
         NEW.projection_artifact_id, NEW.consent_basis, NEW.observed_at, NEW.created_at) IS DISTINCT FROM
        (OLD.tenant_id, OLD.owner_user_id, OLD.task_id, OLD.id, OLD.source_artifact_id,
         OLD.projection_artifact_id, OLD.consent_basis, OLD.observed_at, OLD.created_at) THEN
    RAISE EXCEPTION 'invalid memory mutation' USING ERRCODE = 'check_violation';
  END IF;
  UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1 WHERE tenant_id = NEW.tenant_id
    AND owner_user_id = NEW.owner_user_id AND task_id = NEW.task_id;
  RETURN NEW;
END $$;
