-- 0029_skill_lifecycle: immutable Skill releases, installations, receipts and run pins.
-- Tenant RLS is FORCED on every table. Release rows are immutable except state/revocation fields guarded by the API.
CREATE TABLE agent.skill_releases (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  skill_name text NOT NULL CHECK (skill_name ~ '^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$'),
  version text NOT NULL CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$'),
  content_digest char(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  state text NOT NULL CHECK (state IN ('DISCOVERED','DOWNLOADING','QUARANTINED','VALIDATING','VALIDATED',
    'STAGED_FOR_REVIEW','REJECTED','RETIRED','REVOKED')),
  trust_level text NOT NULL CHECK (trust_level IN ('built_in','user_reviewed','third_party_untrusted')),
  scan_digest char(64) NOT NULL CHECK (scan_digest ~ '^[0-9a-f]{64}$'),
  scan jsonb NOT NULL,
  manifest jsonb NOT NULL,
  publisher_id text,
  publisher_handle text,
  publisher_verified boolean NOT NULL DEFAULT false,
  signature_status text NOT NULL DEFAULT 'absent' CHECK (signature_status IN ('absent','unverified','verified','invalid')),
  source_kind text NOT NULL CHECK (source_kind IN ('builtin','upload','github','clawhub','generated')),
  source_commit_sha char(40),
  artifact_digest char(64),
  created_at timestamptz NOT NULL,
  validated_at timestamptz,
  revoked_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, id, owner_user_id),
  UNIQUE (tenant_id, owner_user_id, content_digest),
  UNIQUE (tenant_id, owner_user_id, skill_name, version)
);
CREATE INDEX skill_releases_tenant_name_idx ON agent.skill_releases (tenant_id, skill_name, created_at DESC);

CREATE TABLE agent.skill_installations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  skill_name text NOT NULL,
  release_id uuid NOT NULL,
  desired text NOT NULL CHECK (desired IN ('disabled','enabled')),
  effective text NOT NULL CHECK (effective IN ('NOT_INSTALLED','INSTALLING','INSTALLED_DISABLED','ENABLING',
    'ENABLED','SUSPENDED','UPDATING','ROLLING_BACK','REMOVING','REMOVED','REVOKED')),
  record_version integer NOT NULL CHECK (record_version > 0),
  activation_generation integer NOT NULL DEFAULT 0 CHECK (activation_generation >= 0),
  policy_revision text NOT NULL,
  approved_release_digest char(64),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  revoked_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, release_id, owner_user_id)
    REFERENCES agent.skill_releases (tenant_id, id, owner_user_id)
);
CREATE UNIQUE INDEX skill_installations_owner_name_uq
  ON agent.skill_installations (tenant_id, owner_user_id, skill_name)
  WHERE effective <> 'REMOVED';

CREATE TABLE agent.skill_installation_history (
  tenant_id uuid NOT NULL,
  installation_id uuid NOT NULL,
  release_id uuid NOT NULL,
  replaced_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, installation_id, release_id),
  FOREIGN KEY (tenant_id, installation_id) REFERENCES agent.skill_installations (tenant_id, id),
  FOREIGN KEY (tenant_id, release_id) REFERENCES agent.skill_releases (tenant_id, id)
);

CREATE TABLE agent.skill_receipts (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  actor_user_id uuid NOT NULL,
  request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  action text NOT NULL CHECK (action IN ('ingest','install','enable','disable','update','rollback','remove','revoke')),
  installation_id uuid,
  release_id uuid,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL,
  UNIQUE (tenant_id, actor_user_id, request_id)
);

CREATE TABLE agent.skill_run_pins (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  installation_id uuid NOT NULL,
  release_id uuid NOT NULL,
  content_digest char(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  activation_generation integer NOT NULL,
  policy_revision text NOT NULL,
  capability_digest char(64) NOT NULL,
  dependency_digest char(64) NOT NULL,
  dependency_snapshot_digest char(64) NOT NULL,
  dependency_snapshot jsonb NOT NULL,
  skill_surface_digest char(64) NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, run_id, installation_id),
  FOREIGN KEY (tenant_id, installation_id) REFERENCES agent.skill_installations (tenant_id, id),
  FOREIGN KEY (tenant_id, release_id) REFERENCES agent.skill_releases (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent.runs (tenant_id, id)
);

CREATE TABLE agent.skill_audit_events (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  actor_user_id uuid NOT NULL,
  action text NOT NULL,
  installation_id uuid,
  release_id uuid,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX skill_audit_tenant_created_idx ON agent.skill_audit_events (tenant_id, created_at DESC);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['skill_releases','skill_installations','skill_installation_history',
                           'skill_receipts','skill_run_pins','skill_audit_events'] LOOP
    EXECUTE format('ALTER TABLE agent.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE agent.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON agent.%I USING (tenant_id = nullif(current_setting(''aiagent.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''aiagent.tenant_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT ON agent.skill_releases TO aiagent_api;
GRANT UPDATE (state, revoked_at) ON agent.skill_releases TO aiagent_api;
GRANT SELECT, INSERT, UPDATE ON agent.skill_installations TO aiagent_api;
GRANT SELECT, INSERT ON agent.skill_receipts, agent.skill_installation_history, agent.skill_run_pins TO aiagent_api;
GRANT INSERT ON agent.skill_audit_events TO aiagent_api;
GRANT SELECT ON agent.skill_releases, agent.skill_installations, agent.skill_run_pins TO aiagent_runtime;
GRANT USAGE, SELECT ON SEQUENCE agent.skill_audit_events_id_seq TO aiagent_api;
