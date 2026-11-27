-- 0029_multimodal_ingestion: MMI upload/artifact/job metadata. PROVISIONAL: table names are MMI-local and do not
-- redefine canonical Artifact/Observation/Evidence contracts. Raw bytes live in an owner-scoped quarantine blob root;
-- only server-generated storage keys are persisted. PR #122 also claims 0029; merge order must renumber one branch.
CREATE TABLE agent.mmi_upload_sessions (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  original_filename text NOT NULL CHECK (length(original_filename) BETWEEN 1 AND 255),
  declared_media_type text NOT NULL CHECK (length(declared_media_type) BETWEEN 1 AND 128),
  expected_bytes bigint NOT NULL CHECK (expected_bytes BETWEEN 1 AND 20971520),
  received_bytes bigint NOT NULL DEFAULT 0 CHECK (received_bytes >= 0),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','completed','aborted')),
  artifact_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, owner_user_id, idempotency_key),
  CHECK (expires_at > created_at),
  CHECK (received_bytes <= expected_bytes),
  CHECK ((state = 'completed') = (artifact_id IS NOT NULL))
);
CREATE TABLE agent.mmi_upload_chunks (
  tenant_id uuid NOT NULL,
  upload_id uuid NOT NULL,
  chunk_index integer NOT NULL CHECK (chunk_index BETWEEN 0 AND 65535),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 1048576),
  chunk_sha256 char(64) NOT NULL CHECK (chunk_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, upload_id, chunk_index),
  FOREIGN KEY (tenant_id, upload_id) REFERENCES agent.mmi_upload_sessions(tenant_id, id) ON DELETE CASCADE
);
CREATE TABLE agent.mmi_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  original_filename text NOT NULL CHECK (length(original_filename) BETWEEN 1 AND 255),
  declared_media_type text NOT NULL CHECK (length(declared_media_type) BETWEEN 1 AND 128),
  detected_format text NOT NULL CHECK (detected_format IN ('pdf','png','jpeg','webp','csv','tsv','xlsx','docx','pptx')),
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  byte_length bigint NOT NULL CHECK (byte_length BETWEEN 1 AND 20971520),
  storage_key text NOT NULL CHECK (storage_key ~ '^[0-9a-f-]{36}/[0-9a-f-]{36}\.bin$'),
  quarantine_state text NOT NULL DEFAULT 'quarantined' CHECK (quarantine_state IN ('quarantined','admitted','revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, storage_key),
  CHECK (expires_at > created_at),
  CHECK ((quarantine_state = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE TABLE agent.mmi_parse_jobs (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  parse_key char(64) NOT NULL CHECK (parse_key ~ '^[0-9a-f]{64}$'),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  request_json jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','leased','succeeded','failed','cancelled')),
  lease_owner text CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 128),
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_expires_at timestamptz,
  outcome_json jsonb,
  document_json jsonb,
  error_code text,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, owner_user_id, parse_key),
  FOREIGN KEY (tenant_id, artifact_id) REFERENCES agent.mmi_artifacts(tenant_id, id),
  CHECK (expires_at > created_at),
  CHECK (status <> 'leased' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
  CHECK (status <> 'succeeded' OR outcome_json IS NOT NULL)
);
CREATE INDEX mmi_parse_jobs_queue ON agent.mmi_parse_jobs (status, lease_expires_at) WHERE status IN ('queued','leased');
CREATE INDEX mmi_artifacts_owner ON agent.mmi_artifacts (tenant_id, owner_user_id, created_at DESC);

ALTER TABLE agent.mmi_upload_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.mmi_upload_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.mmi_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.mmi_parse_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.mmi_upload_sessions, agent.mmi_upload_chunks, agent.mmi_artifacts, agent.mmi_parse_jobs FROM PUBLIC;
CREATE POLICY mmi_upload_tenant ON agent.mmi_upload_sessions TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY mmi_chunk_tenant ON agent.mmi_upload_chunks TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY mmi_artifact_tenant ON agent.mmi_artifacts TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY mmi_job_tenant ON agent.mmi_parse_jobs TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON agent.mmi_upload_sessions, agent.mmi_upload_chunks, agent.mmi_artifacts, agent.mmi_parse_jobs TO aiagent_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.mmi_upload_sessions, agent.mmi_upload_chunks, agent.mmi_artifacts, agent.mmi_parse_jobs TO aiagent_ops;
CREATE POLICY mmi_ops_all ON agent.mmi_upload_sessions TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY mmi_chunk_ops_all ON agent.mmi_upload_chunks TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY mmi_artifact_ops_all ON agent.mmi_artifacts TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY mmi_job_ops_all ON agent.mmi_parse_jobs TO aiagent_ops USING (true) WITH CHECK (true);
GRANT SELECT ON agent.mmi_upload_sessions, agent.mmi_upload_chunks, agent.mmi_artifacts, agent.mmi_parse_jobs TO aiagent_tool_gateway;
CREATE POLICY mmi_artifact_gateway_read ON agent.mmi_artifacts FOR SELECT TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY mmi_job_gateway_read ON agent.mmi_parse_jobs FOR SELECT TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);