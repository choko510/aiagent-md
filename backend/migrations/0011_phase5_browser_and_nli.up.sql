-- 0011_phase5_browser_and_nli: semantic verifier policy mode + browser HTTPS/storageState/Human Takeover state & functions.

ALTER TABLE agent.task_context_policies
  ADD COLUMN IF NOT EXISTS semantic_verifier_mode text NOT NULL DEFAULT 'HEURISTIC'
    CHECK (semantic_verifier_mode IN ('HEURISTIC', 'LLM_GATED'));

DROP FUNCTION IF EXISTS agent.context_policy(uuid, uuid, uuid, uuid, bigint, text);
CREATE FUNCTION agent.context_policy(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text, semantic_verifier_mode text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model, p.semantic_verifier_mode
      FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

DROP FUNCTION IF EXISTS agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text);
CREATE FUNCTION agent.context_policy_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text, semantic_verifier_mode text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model, p.semantic_verifier_mode
      FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner
     FOR SHARE OF p;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

-- ---------------------------------------------------------------- Browser HTTPS, encrypted storageState & Takeover
ALTER TABLE agent.browser_sessions
  ADD COLUMN IF NOT EXISTS current_url text,
  ADD COLUMN IF NOT EXISTS takeover_reason text,
  ADD COLUMN IF NOT EXISTS last_observation_text text,
  ADD COLUMN IF NOT EXISTS last_observation_truncated boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS last_observation_at timestamptz,
  ADD COLUMN IF NOT EXISTS storage_state_nonce bytea,
  ADD COLUMN IF NOT EXISTS storage_state_ciphertext bytea,
  ADD COLUMN IF NOT EXISTS storage_state_key_id text,
  ADD COLUMN IF NOT EXISTS storage_state_sha256 char(64);

ALTER TABLE agent.browser_sessions
  DROP CONSTRAINT IF EXISTS browser_sessions_storage_state_tuple_chk;
ALTER TABLE agent.browser_sessions
  ADD CONSTRAINT browser_sessions_storage_state_tuple_chk CHECK (
    (
      storage_state_nonce IS NULL
      AND storage_state_ciphertext IS NULL
      AND storage_state_key_id IS NULL
      AND storage_state_sha256 IS NULL
    ) OR (
      storage_state_nonce IS NOT NULL
      AND length(storage_state_nonce) = 12
      AND storage_state_ciphertext IS NOT NULL
      AND storage_state_key_id IS NOT NULL
      AND storage_state_sha256 ~ '^[0-9a-f]{64}$'
    )
  );

CREATE FUNCTION agent.browser_session_record_state(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint,
  p_mode text,
  p_worker text,
  p_state text,
  p_current_url text,
  p_takeover_reason text,
  p_obs_text text,
  p_obs_truncated boolean,
  p_update_storage boolean DEFAULT false,
  p_storage_nonce bytea DEFAULT NULL,
  p_storage_ciphertext bytea DEFAULT NULL,
  p_storage_key_id text DEFAULT NULL,
  p_storage_sha256 char(64) DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
BEGIN
  IF p_state NOT IN ('ACTIVE', 'PAUSED', 'RECOVERING', 'NEEDS_LOGIN') THEN
    RAISE EXCEPTION 'INVALID_BROWSER_STATE' USING ERRCODE = 'check_violation';
  END IF;

  PERFORM agent.browser_session_check(
    p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, p_mode, p_worker
  );

  UPDATE agent.browser_sessions s
     SET state = p_state,
         current_url = coalesce(p_current_url, s.current_url),
         takeover_reason = p_takeover_reason,
         last_observation_text = coalesce(p_obs_text, s.last_observation_text),
         last_observation_truncated = CASE WHEN p_obs_text IS NOT NULL THEN p_obs_truncated ELSE s.last_observation_truncated END,
         last_observation_at = CASE WHEN p_obs_text IS NOT NULL THEN now() ELSE s.last_observation_at END,
         storage_state_nonce = CASE WHEN p_update_storage THEN p_storage_nonce ELSE s.storage_state_nonce END,
         storage_state_ciphertext = CASE WHEN p_update_storage THEN p_storage_ciphertext ELSE s.storage_state_ciphertext END,
         storage_state_key_id = CASE WHEN p_update_storage THEN p_storage_key_id ELSE s.storage_state_key_id END,
         storage_state_sha256 = CASE WHEN p_update_storage THEN p_storage_sha256 ELSE s.storage_state_sha256 END,
         updated_at = now()
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.closed_at IS NULL;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows <> 1 THEN
    RAISE EXCEPTION 'STALE_LEASE' USING ERRCODE = 'check_violation';
  END IF;
END $$;

CREATE FUNCTION agent.browser_session_fetch_storage_state(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint,
  p_mode text DEFAULT 'AGENT',
  p_worker text DEFAULT NULL
) RETURNS TABLE(
  storage_state_nonce bytea,
  storage_state_ciphertext bytea,
  storage_state_key_id text,
  storage_state_sha256 char(64)
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  PERFORM agent.browser_session_check(
    p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, p_mode, p_worker
  );
  RETURN QUERY
    SELECT s.storage_state_nonce, s.storage_state_ciphertext, s.storage_state_key_id, s.storage_state_sha256
      FROM agent.browser_sessions s
     WHERE s.tenant_id = p_tenant
       AND s.id = p_session
       AND s.owner_user_id = p_owner
       AND s.closed_at IS NULL;
END $$;

CREATE FUNCTION agent.browser_session_read_status(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_task uuid,
  p_run uuid,
  p_goal bigint,
  p_lease bigint,
  p_mode text DEFAULT 'AGENT',
  p_worker text DEFAULT NULL
) RETURNS TABLE(
  state text,
  mode text,
  generation integer,
  current_url text,
  takeover_reason text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  PERFORM agent.browser_session_check(
    p_tenant, p_session, p_owner, p_task, p_run, p_goal, p_lease, p_mode, p_worker
  );
  RETURN QUERY
    SELECT s.state, s.mode, s.generation, s.current_url, s.takeover_reason
      FROM agent.browser_sessions s
     WHERE s.tenant_id = p_tenant
       AND s.id = p_session
       AND s.owner_user_id = p_owner
       AND s.closed_at IS NULL;
END $$;

CREATE FUNCTION agent.browser_session_takeover_start(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_reason text DEFAULT 'USER_REQUESTED'
) RETURNS TABLE(generation integer, goal_revision bigint, lease_epoch bigint, state text, mode text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  RETURN QUERY
    UPDATE agent.browser_sessions s
       SET mode = 'HUMAN',
           state = 'PAUSED',
           takeover_reason = coalesce(nullif(p_reason, ''), s.takeover_reason, 'USER_REQUESTED'),
           updated_at = now()
      FROM agent.tasks t, agent.runs r
     WHERE s.tenant_id = p_tenant
       AND s.id = p_session
       AND s.owner_user_id = p_owner
       AND s.closed_at IS NULL
       AND s.state IN ('ACTIVE', 'PAUSED', 'NEEDS_LOGIN')
       AND s.restore_incarnation = v_incarnation
       AND t.tenant_id = s.tenant_id
       AND t.id = s.task_id
       AND t.owner_user_id = p_owner
       AND t.active_run_id = s.run_id
       AND r.tenant_id = s.tenant_id
       AND r.id = s.run_id
       AND r.restore_incarnation = v_incarnation
       AND NOT r.cancel_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
   RETURNING s.generation, s.goal_revision, s.lease_epoch, s.state, s.mode;
END $$;

CREATE FUNCTION agent.browser_session_takeover_complete(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid
) RETURNS TABLE(generation integer, goal_revision bigint, lease_epoch bigint, state text, mode text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_incarnation bigint;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  RETURN QUERY
    UPDATE agent.browser_sessions s
       SET mode = 'AGENT',
           state = 'ACTIVE',
           generation = s.generation + 1,
           takeover_reason = NULL,
           updated_at = now()
      FROM agent.tasks t, agent.runs r
     WHERE s.tenant_id = p_tenant
       AND s.id = p_session
       AND s.owner_user_id = p_owner
       AND s.closed_at IS NULL
       AND s.mode = 'HUMAN'
       AND s.state IN ('PAUSED', 'NEEDS_LOGIN')
       AND s.restore_incarnation = v_incarnation
       AND t.tenant_id = s.tenant_id
       AND t.id = s.task_id
       AND t.owner_user_id = p_owner
       AND t.active_run_id = s.run_id
       AND r.tenant_id = s.tenant_id
       AND r.id = s.run_id
       AND r.restore_incarnation = v_incarnation
       AND NOT r.cancel_requested
       AND r.status_code NOT IN ('CANCELLED','CANCELLING','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
       AND agent.scope_active(p_tenant, p_owner, t.workspace_id)
   RETURNING s.generation, s.goal_revision, s.lease_epoch, s.state, s.mode;
END $$;

CREATE FUNCTION agent.browser_session_takeover_set_storage(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid,
  p_storage_nonce bytea,
  p_storage_ciphertext bytea,
  p_storage_key_id text,
  p_storage_sha256 char(64),
  p_obs_text text DEFAULT NULL,
  p_obs_truncated boolean DEFAULT false,
  p_current_url text DEFAULT NULL
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_incarnation bigint;
  v_rows integer;
BEGIN
  SELECT restore_incarnation INTO v_incarnation FROM agent.schema_control WHERE singleton;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'CONTROL_MISSING' USING ERRCODE = 'check_violation';
  END IF;

  UPDATE agent.browser_sessions s
     SET storage_state_nonce = p_storage_nonce,
         storage_state_ciphertext = p_storage_ciphertext,
         storage_state_key_id = p_storage_key_id,
         storage_state_sha256 = p_storage_sha256,
         current_url = coalesce(p_current_url, s.current_url),
         last_observation_text = coalesce(p_obs_text, s.last_observation_text),
         last_observation_truncated = CASE WHEN p_obs_text IS NOT NULL THEN p_obs_truncated ELSE s.last_observation_truncated END,
         last_observation_at = CASE WHEN p_obs_text IS NOT NULL THEN now() ELSE s.last_observation_at END,
         updated_at = now()
    FROM agent.tasks t, agent.runs r
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.closed_at IS NULL
     AND s.mode = 'HUMAN'
     AND s.state IN ('PAUSED', 'NEEDS_LOGIN')
     AND s.restore_incarnation = v_incarnation
     AND t.tenant_id = s.tenant_id
     AND t.id = s.task_id
     AND t.owner_user_id = p_owner
     AND t.active_run_id = s.run_id
     AND r.tenant_id = s.tenant_id
     AND r.id = s.run_id
     AND r.restore_incarnation = v_incarnation
     AND NOT r.cancel_requested
     AND r.status_code NOT IN ('CANCELLED','CANCELLING','FAILED','SUCCEEDED','PARTIAL','EXPIRED','BUDGET_EXHAUSTED')
     AND agent.scope_active(p_tenant, p_owner, t.workspace_id);
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END $$;

CREATE FUNCTION agent.browser_session_takeover_abort(
  p_tenant uuid,
  p_session uuid,
  p_owner uuid
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
BEGIN
  UPDATE agent.browser_sessions s
     SET state = 'CLOSED',
         closed_at = now(),
         takeover_reason = 'ABORTED_BY_OWNER',
         current_url = NULL,
         last_observation_text = NULL,
         last_observation_truncated = false,
         last_observation_at = NULL,
         storage_state_nonce = NULL,
         storage_state_ciphertext = NULL,
         storage_state_key_id = NULL,
         storage_state_sha256 = NULL,
         updated_at = now()
   WHERE s.tenant_id = p_tenant
     AND s.id = p_session
     AND s.owner_user_id = p_owner
     AND s.closed_at IS NULL;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END $$;

REVOKE ALL ON FUNCTION agent.browser_session_record_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text, text, text, text, text, boolean, boolean, bytea, bytea, text, char) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_fetch_storage_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_read_status(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_takeover_start(uuid, uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_takeover_set_storage(uuid, uuid, uuid, bytea, bytea, text, char, text, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_takeover_complete(uuid, uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.browser_session_takeover_abort(uuid, uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION agent.browser_session_record_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text, text, text, text, text, boolean, boolean, bytea, bytea, text, char)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_fetch_storage_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_read_status(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text)
  TO aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_takeover_start(uuid, uuid, uuid, text)
  TO aiagent_api, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_takeover_set_storage(uuid, uuid, uuid, bytea, bytea, text, char, text, boolean, text)
  TO aiagent_api, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_takeover_complete(uuid, uuid, uuid)
  TO aiagent_api, aiagent_ops;
GRANT EXECUTE ON FUNCTION agent.browser_session_takeover_abort(uuid, uuid, uuid)
  TO aiagent_api, aiagent_ops;

CREATE FUNCTION agent.browser_session_wipe_on_close() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'CLOSED' OR NEW.closed_at IS NOT NULL THEN
    NEW.current_url := NULL;
    NEW.last_observation_text := NULL;
    NEW.last_observation_truncated := false;
    NEW.last_observation_at := NULL;
    NEW.storage_state_nonce := NULL;
    NEW.storage_state_ciphertext := NULL;
    NEW.storage_state_key_id := NULL;
    NEW.storage_state_sha256 := NULL;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER browser_session_wipe_closed
  BEFORE UPDATE ON agent.browser_sessions
  FOR EACH ROW EXECUTE FUNCTION agent.browser_session_wipe_on_close();

CREATE TABLE IF NOT EXISTS agent.human_takeover_nonces (
  nonce text PRIMARY KEY,
  tenant_id uuid NOT NULL,
  session_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS human_takeover_nonces_expires_idx ON agent.human_takeover_nonces (expires_at);
ALTER TABLE agent.human_takeover_nonces ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS human_takeover_nonces_sentinel_all ON agent.human_takeover_nonces;
CREATE POLICY human_takeover_nonces_sentinel_all ON agent.human_takeover_nonces
  FOR ALL TO aiagent_sentinel USING (true) WITH CHECK (true);
REVOKE ALL ON agent.human_takeover_nonces FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON agent.human_takeover_nonces TO aiagent_sentinel;
