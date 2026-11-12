-- 0015_memory_consent_history: immutable consent history and external revocation replay.
CREATE TABLE agent.memory_consent_revisions (
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 256),
  revision bigint NOT NULL CHECK (revision >= 1),
  change_kind text NOT NULL CHECK (change_kind IN ('granted','revoked')),
  consent_basis text NOT NULL CHECK (consent_basis IN ('user_explicit','approved_setting','reviewed_import')),
  actor_user_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, owner_user_id, recipient, revision),
  FOREIGN KEY (tenant_id, owner_user_id, recipient)
    REFERENCES agent.memory_consents(tenant_id, owner_user_id, recipient)
);
ALTER TABLE agent.memory_consent_revisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_tenant ON agent.memory_consent_revisions TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY service_all ON agent.memory_consent_revisions TO aiagent_ops USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON agent.memory_consent_revisions TO aiagent_api;
GRANT SELECT ON agent.memory_consent_revisions TO aiagent_ops;
CREATE TRIGGER memory_consent_revision_append_only BEFORE UPDATE OR DELETE
  ON agent.memory_consent_revisions FOR EACH ROW EXECUTE FUNCTION agent.forbid_mutation();
