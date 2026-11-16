-- 0014_context_admission_locks: serialize Context admission with owner policy and
-- recipient-consent withdrawal. Read-only preflight is not an authorization
-- boundary; these row locks are held by the admission transaction until its
-- dispatch row commits.
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
GRANT EXECUTE ON FUNCTION agent.context_policy_lock(uuid, uuid, uuid, uuid, bigint, text)
  TO aiagent_model_gateway;

CREATE FUNCTION agent.memory_consent_lock(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text, p_recipient text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE granted boolean;
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT true INTO granted FROM agent.memory_consents m
   WHERE m.tenant_id = p_tenant AND m.owner_user_id = p_owner AND m.recipient = p_recipient
     AND m.revoked_at IS NULL
   FOR SHARE OF m;
  RETURN coalesce(granted, false);
END $$;
REVOKE ALL ON FUNCTION agent.memory_consent_lock(uuid, uuid, uuid, uuid, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_consent_lock(uuid, uuid, uuid, uuid, bigint, text, text)
  TO aiagent_model_gateway;
