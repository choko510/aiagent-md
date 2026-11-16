-- 0016_memory_tombstone_coverage: make the external tombstone log a *runtime*
-- authority for Memory egress, not only a restore-time one.
--
-- A revocation is appended to the external log and applied to PostgreSQL in the
-- same transaction. This singleton records, from inside that transaction, the
-- newest *revocation* generation whose database effect is committed. At admission
-- the Model Gateway therefore verifies:
--   * the external log is readable and its HMAC chain plus sealed head verify;
--   * the log is not behind the committed watermark (a rolled-back, restored or
--     diverged external volume is detected);
--   * the newest revocation generation in the log is covered by the database
--     (an append whose database transaction failed blocks new Memory egress
--     instead of being treated as already applied).
--
-- `created` entries never need coverage: revocation is permanent for a row and
-- bumps its revision, so a later create cannot revive it.
CREATE TABLE agent.memory_tombstone_coverage (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  covered_revocation_generation bigint NOT NULL DEFAULT 0 CHECK (covered_revocation_generation >= 0),
  chain_head char(64) NOT NULL DEFAULT repeat('0', 64) CHECK (chain_head ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO agent.memory_tombstone_coverage(singleton) VALUES (true);
REVOKE ALL ON agent.memory_tombstone_coverage FROM PUBLIC;
GRANT SELECT, UPDATE ON agent.memory_tombstone_coverage TO aiagent_api, aiagent_ops;

-- Claim-bound read for the gateway: no direct table privilege, and a stale
-- incarnation or dead lease denies before any comparison happens.
CREATE FUNCTION agent.memory_tombstone_coverage_read(
  p_tenant uuid, p_owner uuid, p_task uuid, p_run uuid, p_lease bigint, p_worker text
) RETURNS TABLE(covered_revocation_generation bigint, chain_head char(64))
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
BEGIN
  IF NOT agent.context_claim_valid(p_tenant, p_owner, p_task, p_run, p_lease, p_worker)
     OR NOT agent.memory_restore_ready(p_tenant, p_owner, p_task) THEN
    RAISE EXCEPTION 'context claim denied' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN QUERY
    SELECT c.covered_revocation_generation, c.chain_head
      FROM agent.memory_tombstone_coverage c WHERE c.singleton;
END $$;
REVOKE ALL ON FUNCTION agent.memory_tombstone_coverage_read(uuid, uuid, uuid, uuid, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.memory_tombstone_coverage_read(uuid, uuid, uuid, uuid, bigint, text)
  TO aiagent_model_gateway, aiagent_ops;
