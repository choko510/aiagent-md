-- 0011_phase5_browser_and_nli.down.sql

DROP TRIGGER IF EXISTS browser_session_wipe_closed ON agent.browser_sessions;
DROP FUNCTION IF EXISTS agent.browser_session_wipe_on_close();

REVOKE EXECUTE ON FUNCTION agent.browser_session_takeover_abort(uuid, uuid, uuid) FROM aiagent_api, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_takeover_complete(uuid, uuid, uuid) FROM aiagent_api, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_takeover_set_storage(uuid, uuid, uuid, bytea, bytea, text, char, text, boolean, text) FROM aiagent_api, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_takeover_start(uuid, uuid, uuid, text) FROM aiagent_api, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_read_status(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text)
  FROM aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_fetch_storage_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text)
  FROM aiagent_tool_gateway, aiagent_runtime, aiagent_ops;
REVOKE EXECUTE ON FUNCTION agent.browser_session_record_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text, text, text, text, text, boolean, boolean, bytea, bytea, text, char)
  FROM aiagent_tool_gateway, aiagent_runtime, aiagent_ops;

DROP FUNCTION IF EXISTS agent.browser_session_read_status(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text);

DROP FUNCTION IF EXISTS agent.browser_session_takeover_abort(uuid, uuid, uuid);
DROP FUNCTION IF EXISTS agent.browser_session_takeover_complete(uuid, uuid, uuid);
DROP FUNCTION IF EXISTS agent.browser_session_takeover_set_storage(uuid, uuid, uuid, bytea, bytea, text, char, text, boolean, text);
DROP FUNCTION IF EXISTS agent.browser_session_takeover_start(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS agent.browser_session_fetch_storage_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text);
DROP FUNCTION IF EXISTS agent.browser_session_record_state(uuid, uuid, uuid, uuid, uuid, bigint, bigint, text, text, text, text, text, text, boolean, boolean, bytea, bytea, text, char);

ALTER TABLE agent.browser_sessions
  DROP CONSTRAINT IF EXISTS browser_sessions_storage_state_tuple_chk,
  DROP COLUMN IF EXISTS storage_state_sha256,
  DROP COLUMN IF EXISTS storage_state_key_id,
  DROP COLUMN IF EXISTS storage_state_ciphertext,
  DROP COLUMN IF EXISTS storage_state_nonce,
  DROP COLUMN IF EXISTS last_observation_at,
  DROP COLUMN IF EXISTS last_observation_truncated,
  DROP COLUMN IF EXISTS last_observation_text,
  DROP COLUMN IF EXISTS takeover_reason,
  DROP COLUMN IF EXISTS current_url;

DROP FUNCTION IF EXISTS agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text);
CREATE FUNCTION agent.context_policy_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model
      FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner
     FOR SHARE OF p;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

DROP FUNCTION IF EXISTS agent.context_policy(uuid, uuid, uuid, uuid, bigint, text);
CREATE FUNCTION agent.context_policy(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(enabled boolean, recipient text, model text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT p.enabled, p.recipient, p.model FROM agent.task_context_policies p
     WHERE p.tenant_id = p_tenant AND p.task_id = p_task AND p.owner_user_id = p_owner;
END $$;
REVOKE ALL ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.context_policy(uuid, uuid, uuid, uuid, bigint, text) TO aiagent_model_gateway;

ALTER TABLE agent.task_context_policies
  DROP COLUMN IF EXISTS semantic_verifier_mode;
