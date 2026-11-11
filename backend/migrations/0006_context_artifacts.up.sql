-- Provisional internal Context storage; ciphertext and metadata commit atomically in PostgreSQL.
-- No API or model dispatch is enabled by installing this migration.
CREATE TABLE agent.context_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('raw', 'summary')),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 2000000),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext bytea,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id, run_id) REFERENCES agent.runs(tenant_id, task_id, id),
  CHECK (expires_at > created_at),
  CHECK ((revoked_at IS NULL) = (ciphertext IS NOT NULL))
);
CREATE INDEX context_artifacts_expiry ON agent.context_artifacts (expires_at) WHERE revoked_at IS NULL;
ALTER TABLE agent.context_artifacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY context_artifacts_model_gateway ON agent.context_artifacts TO aiagent_model_gateway
  USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON agent.context_artifacts TO aiagent_model_gateway;
GRANT UPDATE (revoked_at, ciphertext) ON agent.context_artifacts TO aiagent_model_gateway;
GRANT SELECT ON agent.context_artifacts TO aiagent_ops;
-- Expired ciphertext is erased by the privileged retention operator; MVCC/WAL and
-- backups have independent retention and must not be reported as erased here.
CREATE FUNCTION agent.gc_expired_context_artifacts() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE n integer;
BEGIN
  UPDATE agent.context_artifacts SET revoked_at=clock_timestamp(), ciphertext=NULL
   WHERE revoked_at IS NULL AND expires_at <= clock_timestamp();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION agent.gc_expired_context_artifacts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.gc_expired_context_artifacts() TO aiagent_ops;
