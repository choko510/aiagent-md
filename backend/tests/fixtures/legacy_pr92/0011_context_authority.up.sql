-- 0011_context_authority: DB-enforced Context/Memory authority.
-- The Model Gateway role may no longer read or write artifact/memory/dispatch rows
-- directly. Every operation goes through a SECURITY DEFINER function that validates
-- the current Run claim (tenant/owner/task/run/lease/worker/restore) itself, so a
-- client-set GUC is not an authorization boundary. key_id binds ciphertext to a key
-- in the deployment key ring.

ALTER TABLE agent.context_artifacts ADD COLUMN key_id text NOT NULL DEFAULT 'legacy'
  CHECK (key_id ~ '^[A-Za-z0-9._-]{1,64}$');

DROP POLICY IF EXISTS context_artifacts_read_scope ON agent.context_artifacts;
DROP POLICY IF EXISTS context_artifacts_insert_scope ON agent.context_artifacts;
DROP POLICY IF EXISTS context_artifacts_update_scope ON agent.context_artifacts;
DROP POLICY IF EXISTS context_artifacts_model_gateway ON agent.context_artifacts;
REVOKE ALL ON agent.context_artifacts FROM aiagent_model_gateway;

REVOKE ALL ON agent.memory_scope_epochs, agent.memory_records FROM aiagent_model_gateway;
REVOKE ALL ON agent.context_dispatches FROM aiagent_model_gateway;
DROP POLICY IF EXISTS context_dispatch_scope ON agent.context_dispatches;
DROP POLICY IF EXISTS memory_epoch_scope ON agent.memory_scope_epochs;
DROP POLICY IF EXISTS memory_record_scope ON agent.memory_records;
-- The authenticated user API remains RLS-scoped (it sets the scope from an
-- already-authenticated request, and owner predicates are additionally in SQL).
CREATE POLICY memory_epoch_scope ON agent.memory_scope_epochs TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));
CREATE POLICY memory_record_scope ON agent.memory_records TO aiagent_api
  USING (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true))
  WITH CHECK (tenant_id::text = current_setting('aiagent.memory_tenant', true)
     AND owner_user_id::text = current_setting('aiagent.memory_owner', true)
     AND task_id::text = current_setting('aiagent.memory_task', true));

-- Shared canonical authority predicate (SQL sibling of control.scope_active).
CREATE FUNCTION agent.scope_active(p_tenant uuid, p_owner uuid, p_workspace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (
    SELECT 1 FROM agent.tenants t
      JOIN agent.tenant_memberships m ON m.tenant_id = t.id AND m.user_id = p_owner
      JOIN agent.users u ON u.id = m.user_id
      JOIN agent.workspaces w ON w.tenant_id = t.id AND w.owner_user_id = m.user_id AND w.id = p_workspace
     WHERE t.id = p_tenant AND t.disabled_at IS NULL AND u.disabled_at IS NULL
       AND m.disabled_at IS NULL AND w.disabled_at IS NULL)
$$;
REVOKE ALL ON FUNCTION agent.scope_active(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.scope_active(uuid, uuid, uuid) TO
  aiagent_api, aiagent_runtime, aiagent_model_gateway, aiagent_tool_gateway, aiagent_sentinel, aiagent_ops;

CREATE FUNCTION agent.context_claim_valid(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (
    SELECT 1 FROM agent.runs r
      JOIN agent.tasks t ON t.tenant_id = r.tenant_id AND t.id = r.task_id
      CROSS JOIN agent.schema_control s
     WHERE r.tenant_id = p_tenant AND r.id = p_run AND r.task_id = p_task
       AND t.owner_user_id = p_owner AND t.active_run_id = p_run
       AND r.lease_epoch = p_lease AND r.lease_owner_ref = p_worker
       AND r.lease_expires_at > clock_timestamp()
       AND r.status_code NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED',
                                 'BUDGET_EXHAUSTED','CANCELLING','PAUSED')
       AND NOT r.cancel_requested AND NOT r.pause_requested
       AND r.restore_incarnation = s.restore_incarnation
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id))
$$;
REVOKE ALL ON FUNCTION agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_claim_valid(uuid, uuid, uuid, uuid, bigint, text) TO
  aiagent_model_gateway, aiagent_ops;

-- ---------------------------------------------------------------- restore watermark state
-- Written only by the restore reconciliation operator flow (aiagent_core.admin memory-reconcile).
-- Absence of a row for the current incarnation means Memory stays denied after a restore.
CREATE TABLE agent.memory_restore_state (
  restore_incarnation bigint PRIMARY KEY CHECK (restore_incarnation >= 1),
  external_generation bigint NOT NULL CHECK (external_generation >= 1),
  tombstone_chain_head char(64) NOT NULL CHECK (tombstone_chain_head ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz NOT NULL,
  reconciled_at timestamptz NOT NULL DEFAULT now(),
  reconciled_by text NOT NULL CHECK (length(reconciled_by) BETWEEN 1 AND 200)
);
REVOKE ALL ON agent.memory_restore_state FROM PUBLIC;
GRANT SELECT ON agent.memory_restore_state TO aiagent_api, aiagent_model_gateway, aiagent_ops;
GRANT INSERT, UPDATE ON agent.memory_restore_state TO aiagent_ops;

-- ---------------------------------------------------------------- artifacts (claim-bound)
CREATE FUNCTION agent.context_artifact_put(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_kind text, p_sha text, p_length integer, p_nonce bytea, p_ciphertext bytea,
  p_expires_at timestamptz, p_key_id text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  INSERT INTO agent.context_artifacts(tenant_id, id, task_id, run_id, owner_user_id, kind, content_sha256,
    byte_length, nonce, ciphertext, expires_at, key_id)
  VALUES (p_tenant, p_id, p_task, p_run, p_owner, p_kind, p_sha, p_length, p_nonce, p_ciphertext, p_expires_at, p_key_id);
  RETURN p_id;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer,
  bytea, bytea, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_put(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text, integer,
  bytea, bytea, timestamptz, text) TO aiagent_model_gateway;

-- User-originated Memory artifacts: authenticated owner scope, attached to the task's active run.
CREATE FUNCTION agent.context_artifact_put_user(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_id uuid, p_kind text, p_sha text, p_length integer,
  p_nonce bytea, p_ciphertext bytea, p_expires_at timestamptz, p_key_id text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  PERFORM 1 FROM agent.tasks t
   WHERE t.tenant_id = p_tenant AND t.id = p_task AND t.owner_user_id = p_owner
     AND t.active_run_id = p_run AND agent.scope_active(p_tenant, p_owner, t.workspace_id);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'memory scope denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_expires_at <= clock_timestamp() THEN RETURN NULL; END IF;
  INSERT INTO agent.context_artifacts(tenant_id, id, task_id, run_id, owner_user_id, kind, content_sha256,
    byte_length, nonce, ciphertext, expires_at, key_id)
  VALUES (p_tenant, p_id, p_task, p_run, p_owner, p_kind, p_sha, p_length, p_nonce, p_ciphertext,
          p_expires_at, p_key_id);
  RETURN p_id;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea, bytea,
  timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_put_user(uuid, uuid, uuid, uuid, uuid, text, text, integer, bytea,
  bytea, timestamptz, text) TO aiagent_model_gateway, aiagent_api;

CREATE FUNCTION agent.context_artifact_fetch(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid, p_kind text
) RETURNS TABLE(run_id uuid, content_sha256 char(64), byte_length integer, nonce bytea, ciphertext bytea, key_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT a.run_id, a.content_sha256, a.byte_length, a.nonce, a.ciphertext, a.key_id
      FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.kind = p_kind AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp();
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_fetch(uuid, uuid, uuid, uuid, bigint, text, uuid, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_present(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_kind text, p_sha text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (
    SELECT 1 FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.kind = p_kind AND a.content_sha256 = p_sha
       AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_present(uuid, uuid, uuid, uuid, bigint, text, uuid, text, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_live(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (
    SELECT 1 FROM agent.context_artifacts a
     WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
       AND a.id = p_id AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_live(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_artifact_revoke(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id) VALUES (p_tenant, p_owner, p_task)
    ON CONFLICT DO NOTHING;
  PERFORM 1 FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task FOR UPDATE;
  UPDATE agent.context_artifacts SET revoked_at = clock_timestamp(), ciphertext = NULL
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task
     AND id = p_id AND revoked_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1
     WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  END IF;
  RETURN n > 0;
END $$;
REVOKE ALL ON FUNCTION agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_artifact_revoke(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

-- ---------------------------------------------------------------- Memory (claim-bound reads)
CREATE FUNCTION agent.memory_restore_ready(p_tenant uuid, p_owner uuid, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT coalesce((SELECT s.restore_incarnation = 1 OR EXISTS (
      SELECT 1 FROM agent.memory_restore_state m WHERE m.restore_incarnation = s.restore_incarnation)
    FROM agent.schema_control s), false)
$$;
REVOKE ALL ON FUNCTION agent.memory_restore_ready(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_restore_ready(uuid, uuid, uuid) TO
  aiagent_api, aiagent_model_gateway, aiagent_ops;

CREATE FUNCTION agent.memory_epoch(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE e bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO e FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  IF e IS NULL THEN
    RAISE EXCEPTION 'memory policy unavailable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN e;
END $$;
REVOKE ALL ON FUNCTION agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_epoch(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_epoch_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_epoch bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE actual bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO actual FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task FOR UPDATE;
  RETURN coalesce(actual = p_epoch, false);
END $$;
REVOKE ALL ON FUNCTION agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_epoch_lock(uuid, uuid, uuid, uuid, bigint, text, bigint)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_live_rows(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(id uuid, source_artifact_id uuid, projection_artifact_id uuid, recipient text,
                consent_basis text, observed_at timestamptz, expires_at timestamptz, revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT m.id, m.source_artifact_id, m.projection_artifact_id, m.recipient, m.consent_basis,
           m.observed_at, m.expires_at, m.revision
      FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id)
     ORDER BY m.created_at DESC, m.id;
END $$;
REVOKE ALL ON FUNCTION agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_live_rows(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

-- Metadata-only artifact usability helper (no ciphertext), independent of client GUCs.
CREATE FUNCTION agent.memory_artifact_ok(p_tenant uuid, p_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT EXISTS (SELECT 1 FROM agent.context_artifacts a
    WHERE a.tenant_id = p_tenant AND a.id = p_id AND a.kind = 'raw'
      AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp())
$$;
REVOKE ALL ON FUNCTION agent.memory_artifact_ok(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_artifact_ok(uuid, uuid) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_row_current(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_id uuid, p_revision bigint, p_recipient text, p_epoch bigint
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE actual bigint;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT source_epoch INTO actual FROM agent.memory_scope_epochs
   WHERE tenant_id = p_tenant AND owner_user_id = p_owner AND task_id = p_task;
  RETURN coalesce(actual = p_epoch, false) AND EXISTS (
    SELECT 1 FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.id = p_id AND m.revision = p_revision AND m.recipient = p_recipient
       AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id));
END $$;
REVOKE ALL ON FUNCTION agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_row_current(uuid, uuid, uuid, uuid, bigint, text, uuid, bigint, text, bigint)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_row(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS TABLE(id uuid, source_artifact_id uuid, projection_artifact_id uuid, recipient text,
                consent_basis text, observed_at timestamptz, expires_at timestamptz, revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT m.id, m.source_artifact_id, m.projection_artifact_id, m.recipient, m.consent_basis,
           m.observed_at, m.expires_at, m.revision
      FROM agent.memory_records m
     WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.task_id = p_task
       AND m.id = p_id AND m.revoked_at IS NULL AND m.expires_at > clock_timestamp()
       AND agent.memory_artifact_ok(m.tenant_id, m.source_artifact_id)
       AND agent.memory_artifact_ok(m.tenant_id, m.projection_artifact_id);
END $$;
REVOKE ALL ON FUNCTION agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_row(uuid, uuid, uuid, uuid, bigint, text, uuid) TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_source_usable(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_id uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (SELECT 1 FROM agent.context_artifacts a
    WHERE a.tenant_id = p_tenant AND a.owner_user_id = p_owner AND a.task_id = p_task
      AND a.id = p_id AND a.kind = 'raw' AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_source_usable(uuid, uuid, uuid, uuid, bigint, text, uuid)
  TO aiagent_model_gateway;

-- ---------------------------------------------------------------- dispatch (claim-bound)
CREATE FUNCTION agent.context_dispatch_record(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_head bigint, p_attempt uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM agent.model_attempts a
     WHERE a.tenant_id = p_tenant AND a.task_id = p_task AND a.run_id = p_run
       AND a.id = p_attempt AND a.run_lease_epoch = p_lease AND a.admitted
  ) THEN
    RETURN false;
  END IF;
  INSERT INTO agent.context_dispatches(tenant_id, owner_user_id, task_id, run_id, head_revision, model_attempt_id,
    status_code) VALUES (p_tenant, p_owner, p_task, p_run, p_head, p_attempt, 'ADMITTED')
  ON CONFLICT DO NOTHING;
  RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_dispatch_record(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.context_dispatch_finish(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text,
  p_head bigint, p_attempt uuid, p_status text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE a record; n integer;
BEGIN
  IF p_status NOT IN ('COMPLETED','UNKNOWN') THEN
    RAISE EXCEPTION 'invalid dispatch status' USING ERRCODE = 'check_violation';
  END IF;
  SELECT d.model_attempt_id, ma.request_identity
    INTO a
    FROM agent.context_dispatches d
    JOIN agent.model_attempts ma ON ma.tenant_id=d.tenant_id AND ma.id=d.model_attempt_id
   WHERE d.tenant_id=p_tenant AND d.owner_user_id=p_owner AND d.task_id=p_task AND d.run_id=p_run
     AND d.head_revision=p_head AND d.model_attempt_id=p_attempt AND d.status_code='ADMITTED'
     AND ma.task_id=p_task AND ma.run_id=p_run AND ma.run_lease_epoch=p_lease
   FOR UPDATE OF d;
  IF NOT FOUND THEN RETURN false; END IF;
  IF p_status='COMPLETED' THEN
    IF NOT agent.context_claim_valid(p_tenant,p_owner,p_task,p_run,p_lease,p_worker)
       OR NOT EXISTS (
         SELECT 1 FROM agent.run_checkpoints cp
          WHERE cp.tenant_id=p_tenant AND cp.task_id=p_task AND cp.run_id=p_run
            AND cp.stream='main' AND cp.entry_kind='assistant'
            AND cp.request_key=a.request_identity AND cp.checkpoint_revision>p_head
       ) THEN
      RETURN false;
    END IF;
  END IF;
  UPDATE agent.context_dispatches SET status_code=p_status
   WHERE tenant_id=p_tenant AND owner_user_id=p_owner AND task_id=p_task AND run_id=p_run
     AND head_revision=p_head AND model_attempt_id=p_attempt AND status_code='ADMITTED';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;
END $$;
REVOKE ALL ON FUNCTION agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_dispatch_finish(uuid, uuid, uuid, uuid, bigint, text, bigint, uuid, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.gc_orphan_context_artifacts(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE r record; n integer; total integer := 0;
BEGIN
  FOR r IN
    SELECT DISTINCT a.tenant_id, a.owner_user_id, a.task_id
      FROM agent.context_artifacts a
      JOIN agent.runs ru ON ru.tenant_id = a.tenant_id AND ru.id = a.run_id
     WHERE a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
       AND a.created_at < clock_timestamp() - older_than
       AND ru.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
       AND NOT EXISTS (SELECT 1 FROM agent.memory_records m
             WHERE m.tenant_id = a.tenant_id AND m.revoked_at IS NULL
               AND m.expires_at > clock_timestamp()
               AND a.id IN (m.source_artifact_id, m.projection_artifact_id))
       AND NOT EXISTS (SELECT 1 FROM agent.run_checkpoints cp
             WHERE cp.tenant_id = a.tenant_id AND cp.run_id = a.run_id AND cp.entry_kind = 'summary'
               AND (cp.checkpoint_json->>'payloadRef' = a.id::text
                    OR cp.checkpoint_json->>'summaryRef' = a.id::text
                    OR cp.checkpoint_json->'rawRefs' ? a.id::text))
     ORDER BY a.tenant_id, a.owner_user_id, a.task_id LOOP
    INSERT INTO agent.memory_scope_epochs(tenant_id, owner_user_id, task_id)
      VALUES (r.tenant_id, r.owner_user_id, r.task_id) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM agent.memory_scope_epochs
     WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id FOR UPDATE;
    UPDATE agent.context_artifacts a SET revoked_at = clock_timestamp(), ciphertext = NULL
      FROM agent.runs ru
     WHERE a.tenant_id = r.tenant_id AND a.owner_user_id = r.owner_user_id AND a.task_id = r.task_id
       AND ru.tenant_id = a.tenant_id AND ru.id = a.run_id
       AND a.revoked_at IS NULL AND a.expires_at > clock_timestamp()
       AND a.created_at < clock_timestamp() - older_than
       AND ru.status_code IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','EXPIRED','BUDGET_EXHAUSTED')
       AND NOT EXISTS (SELECT 1 FROM agent.memory_records m
             WHERE m.tenant_id = a.tenant_id AND m.revoked_at IS NULL
               AND m.expires_at > clock_timestamp()
               AND a.id IN (m.source_artifact_id, m.projection_artifact_id))
       AND NOT EXISTS (SELECT 1 FROM agent.run_checkpoints cp
             WHERE cp.tenant_id = a.tenant_id AND cp.run_id = a.run_id AND cp.entry_kind = 'summary'
               AND (cp.checkpoint_json->>'payloadRef' = a.id::text
                    OR cp.checkpoint_json->>'summaryRef' = a.id::text
                    OR cp.checkpoint_json->'rawRefs' ? a.id::text));
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
      UPDATE agent.memory_scope_epochs SET source_epoch = source_epoch + 1
       WHERE tenant_id = r.tenant_id AND owner_user_id = r.owner_user_id AND task_id = r.task_id;
      total := total + n;
    END IF;
  END LOOP;
  RETURN total;
END $$;
REVOKE ALL ON FUNCTION agent.gc_orphan_context_artifacts(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.gc_orphan_context_artifacts(interval) TO aiagent_ops;

-- Legacy GUC-based helpers: never reachable from the Model Gateway any more.
REVOKE EXECUTE ON FUNCTION agent.memory_artifact_usable(uuid, uuid, uuid, uuid) FROM aiagent_model_gateway;
REVOKE EXECUTE ON FUNCTION agent.lock_memory_epoch(uuid, uuid, uuid, bigint) FROM aiagent_model_gateway;
REVOKE EXECUTE ON FUNCTION agent.revoke_context_artifact(uuid, uuid, uuid, uuid, uuid) FROM aiagent_model_gateway;
