-- Existing bindings remain read-only for Git commits until an operator explicitly
-- provisions a branch. Changing a binding already requires a new generation.
ALTER TABLE agent.repository_bindings ADD COLUMN approved_branch text;
ALTER TABLE agent.repository_bindings ADD CONSTRAINT repository_binding_branch_valid CHECK (
  approved_branch IS NULL OR (
    length(approved_branch) BETWEEN 1 AND 200
    AND approved_branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
    AND lower(approved_branch) NOT IN ('main', 'master', 'head')
    AND approved_branch NOT LIKE '%..%'
    AND approved_branch NOT LIKE '%//%'
    AND approved_branch NOT LIKE '%/'
    AND approved_branch NOT LIKE '%.'
    AND approved_branch !~ '(^|/)\.'
    AND approved_branch !~ '\.lock(/|$)'
  )
);

-- Patch bytes are encrypted by the trusted artifact owner before insertion.
-- Only an operator can register or revoke an artifact; a model cannot approve it.
CREATE TABLE agent.git_patch_artifacts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  binding_id uuid NOT NULL,
  binding_generation bigint NOT NULL CHECK (binding_generation >= 1),
  cycle_id text NOT NULL,
  task_id uuid NOT NULL,
  run_id uuid NOT NULL,
  parent_sha text NOT NULL CHECK (parent_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  diff_sha256 text NOT NULL CHECK (diff_sha256 ~ '^[0-9a-f]{64}$'),
  paths text[] NOT NULL CHECK (cardinality(paths) BETWEEN 1 AND 512),
  byte_length integer NOT NULL CHECK (byte_length BETWEEN 1 AND 4194304),
  key_id text NOT NULL CHECK (length(key_id) BETWEEN 1 AND 64),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) = byte_length + 16),
  active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (key_id, nonce),
  FOREIGN KEY (tenant_id, workspace_id, binding_id)
    REFERENCES agent.repository_bindings(tenant_id, workspace_id, id),
  FOREIGN KEY (tenant_id, cycle_id) REFERENCES agent.dev_cycles(tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent.tasks(tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent.runs(tenant_id, id)
);

CREATE FUNCTION agent.git_patch_artifact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (to_jsonb(NEW) - 'active') IS DISTINCT FROM (to_jsonb(OLD) - 'active')
     OR NOT OLD.active OR NEW.active THEN
    RAISE EXCEPTION 'IMMUTABLE_PATCH_ARTIFACT' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER git_patch_artifact_update_guard BEFORE UPDATE ON agent.git_patch_artifacts
  FOR EACH ROW EXECUTE FUNCTION agent.git_patch_artifact_guard();
ALTER TABLE agent.git_patch_artifacts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.git_patch_artifacts FROM PUBLIC;
CREATE POLICY git_patch_ops ON agent.git_patch_artifacts TO aiagent_ops USING (true) WITH CHECK (true);
CREATE POLICY git_patch_gateway ON agent.git_patch_artifacts FOR SELECT TO aiagent_tool_gateway
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON agent.git_patch_artifacts TO aiagent_ops;
GRANT SELECT ON agent.git_patch_artifacts TO aiagent_tool_gateway;
