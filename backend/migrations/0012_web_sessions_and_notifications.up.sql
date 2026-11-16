-- 0012_web_sessions_and_notifications: HttpOnly Web sessions (CSRF-bound) + Notification bindings, intents, attempts & delivery nonces.

CREATE TABLE agent.web_sessions (
  id uuid NOT NULL PRIMARY KEY,
  session_sha256 char(64) NOT NULL UNIQUE CHECK (session_sha256 ~ '^[0-9a-f]{64}$'),
  csrf_token text NOT NULL CHECK (length(csrf_token) BETWEEN 32 AND 128),
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  FOREIGN KEY (tenant_id, workspace_id, user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id)
);
CREATE INDEX web_sessions_expires_idx ON agent.web_sessions (expires_at);
CREATE INDEX web_sessions_owner_idx ON agent.web_sessions (tenant_id, user_id, expires_at DESC);

ALTER TABLE agent.web_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.web_sessions FROM PUBLIC;
CREATE POLICY web_sessions_api_tenant ON agent.web_sessions TO aiagent_api
  USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid);
CREATE POLICY web_sessions_ops_all ON agent.web_sessions TO aiagent_ops USING (true) WITH CHECK (true);
GRANT SELECT ON agent.web_sessions TO aiagent_ops;

CREATE FUNCTION agent.create_web_session(
  p_token_sha256 text,
  p_session_id uuid,
  p_session_sha256 text,
  p_csrf_token text,
  p_ttl_secs integer
) RETURNS TABLE(session_id uuid, tenant_id uuid, user_id uuid, workspace_id uuid, csrf_token text, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_tenant uuid;
  v_user uuid;
  v_workspace uuid;
  v_expires timestamptz;
BEGIN
  IF p_ttl_secs IS NULL OR p_ttl_secs < 60 OR p_ttl_secs > 604800 THEN
    RAISE EXCEPTION 'invalid session ttl' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT c.tenant_id, c.user_id, c.workspace_id
    INTO v_tenant, v_user, v_workspace
    FROM agent.api_credentials c
    JOIN agent.tenant_memberships m ON m.tenant_id = c.tenant_id AND m.user_id = c.user_id
    JOIN agent.tenants t ON t.id = c.tenant_id
    JOIN agent.users u ON u.id = c.user_id
    JOIN agent.workspaces w ON w.tenant_id = c.tenant_id AND w.id = c.workspace_id AND w.owner_user_id = c.user_id
   WHERE c.token_sha256 = p_token_sha256
     AND c.revoked_at IS NULL
     AND (c.expires_at IS NULL OR c.expires_at > clock_timestamp())
     AND m.disabled_at IS NULL
     AND t.disabled_at IS NULL
     AND u.disabled_at IS NULL
     AND w.disabled_at IS NULL
   LIMIT 1;
  IF v_tenant IS NULL THEN
    RETURN;
  END IF;
  v_expires := clock_timestamp() + make_interval(secs => p_ttl_secs);
  INSERT INTO agent.web_sessions(id, session_sha256, csrf_token, tenant_id, user_id, workspace_id, expires_at)
  VALUES (p_session_id, p_session_sha256, p_csrf_token, v_tenant, v_user, v_workspace, v_expires);
  RETURN QUERY SELECT p_session_id, v_tenant, v_user, v_workspace, p_csrf_token, v_expires;
END $$;

CREATE FUNCTION agent.resolve_web_session(p_session_sha256 text)
RETURNS TABLE(session_id uuid, tenant_id uuid, user_id uuid, workspace_id uuid, csrf_token text, expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
  SELECT s.id, s.tenant_id, s.user_id, s.workspace_id, s.csrf_token, s.expires_at
    FROM agent.web_sessions s
    JOIN agent.tenant_memberships m ON m.tenant_id = s.tenant_id AND m.user_id = s.user_id
    JOIN agent.tenants t ON t.id = s.tenant_id
    JOIN agent.users u ON u.id = s.user_id
    JOIN agent.workspaces w ON w.tenant_id = s.tenant_id AND w.id = s.workspace_id AND w.owner_user_id = s.user_id
   WHERE s.session_sha256 = p_session_sha256
     AND s.revoked_at IS NULL
     AND s.expires_at > clock_timestamp()
     AND m.disabled_at IS NULL
     AND t.disabled_at IS NULL
     AND u.disabled_at IS NULL
     AND w.disabled_at IS NULL
$$;

CREATE FUNCTION agent.revoke_web_session(p_session_sha256 text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, agent AS $$
DECLARE
  v_rows integer;
BEGIN
  UPDATE agent.web_sessions
     SET revoked_at = clock_timestamp()
   WHERE session_sha256 = p_session_sha256 AND revoked_at IS NULL;
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END $$;

REVOKE ALL ON FUNCTION agent.create_web_session(text, uuid, text, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.resolve_web_session(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION agent.revoke_web_session(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.create_web_session(text, uuid, text, text, integer) TO aiagent_api;
GRANT EXECUTE ON FUNCTION agent.resolve_web_session(text) TO aiagent_api;
GRANT EXECUTE ON FUNCTION agent.revoke_web_session(text) TO aiagent_api;

-- ---------------------------------------------------------------- Notification Bindings & Outbox Delivery Ledger (NOT-001..016)
CREATE TABLE agent.notification_bindings (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('webhook', 'telegram')),
  label text NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
  destination_scheme text NOT NULL CHECK (destination_scheme IN ('https', 'http')),
  destination_host text NOT NULL CHECK (destination_host ~ '^[a-z0-9.-]{1,253}$'),
  destination_port integer NOT NULL CHECK (destination_port BETWEEN 1 AND 65535),
  redacted_target text NOT NULL CHECK (length(redacted_target) BETWEEN 1 AND 256),
  secret_nonce bytea NOT NULL,
  secret_ciphertext bytea NOT NULL,
  secret_key_id text NOT NULL,
  secret_sha256 char(64) NOT NULL CHECK (secret_sha256 ~ '^[0-9a-f]{64}$'),
  binding_version bigint NOT NULL DEFAULT 1 CHECK (binding_version >= 1),
  consent_granted boolean NOT NULL DEFAULT false,
  consent_version bigint NOT NULL DEFAULT 1 CHECK (consent_version >= 1),
  quiet_start_hour smallint CHECK (quiet_start_hour BETWEEN 0 AND 23),
  quiet_end_hour smallint CHECK (quiet_end_hour BETWEEN 0 AND 23),
  timezone_id text NOT NULL DEFAULT 'UTC' CHECK (length(timezone_id) BETWEEN 1 AND 64),
  opted_out_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id),
  CHECK ((quiet_start_hour IS NULL AND quiet_end_hour IS NULL) OR (quiet_start_hour IS NOT NULL AND quiet_end_hour IS NOT NULL))
);
CREATE INDEX notification_bindings_owner_idx ON agent.notification_bindings (tenant_id, owner_user_id, created_at DESC);

CREATE TABLE agent.notification_intents (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  binding_id uuid NOT NULL,
  binding_version bigint NOT NULL CHECK (binding_version >= 1),
  consent_version bigint NOT NULL CHECK (consent_version >= 1),
  semantic_event_key text NOT NULL CHECK (length(semantic_event_key) BETWEEN 1 AND 200),
  delivery_purpose text NOT NULL DEFAULT 'alert' CHECK (length(delivery_purpose) BETWEEN 1 AND 64),
  source_kind text NOT NULL CHECK (source_kind IN ('watch', 'task', 'campaign', 'system')),
  source_ref text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 200),
  evidence_urls_json jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_urls_json) = 'array'),
  observed_at timestamptz NOT NULL DEFAULT now(),
  priority text NOT NULL DEFAULT 'standard' CHECK (priority IN ('critical', 'standard', 'low')),
  title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  summary_text text NOT NULL CHECK (length(summary_text) BETWEEN 1 AND 4000),
  status text NOT NULL DEFAULT 'QUEUED' CHECK (
    status IN ('QUEUED','DEFERRED_QUIET_HOURS','SENDING','PROVIDER_ACCEPTED','DELIVERY_UNKNOWN','FAILED','SUPPRESSED','READ')
  ),
  not_before_at timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_owner_ref text,
  lease_until timestamptz,
  restore_incarnation bigint NOT NULL DEFAULT 1 CHECK (restore_incarnation >= 1),
  last_error_code text,
  provider_receipt_ref text,
  delivered_at timestamptz,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, owner_user_id, binding_id, semantic_event_key, delivery_purpose),
  FOREIGN KEY (tenant_id, binding_id) REFERENCES agent.notification_bindings(tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id, owner_user_id) REFERENCES agent.workspaces(tenant_id, id, owner_user_id)
);
CREATE INDEX notification_intents_ready_idx
  ON agent.notification_intents (next_attempt_at, not_before_at, id)
  WHERE status IN ('QUEUED', 'DEFERRED_QUIET_HOURS');
CREATE INDEX notification_intents_owner_idx
  ON agent.notification_intents (tenant_id, owner_user_id, created_at DESC);

CREATE TABLE agent.notification_attempts (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  intent_id uuid NOT NULL,
  binding_id uuid NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  send_token text NOT NULL CHECK (length(send_token) BETWEEN 8 AND 128),
  claimed_lease_epoch bigint NOT NULL CHECK (claimed_lease_epoch >= 1),
  disposition text NOT NULL CHECK (
    disposition IN ('NOT_STARTED','TRANSPORT_REJECTED','PROVIDER_ACCEPTED','UNKNOWN','PERMANENT_FAILURE')
  ),
  provider_receipt_ref text,
  error_code text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, intent_id, attempt_no),
  FOREIGN KEY (tenant_id, intent_id) REFERENCES agent.notification_intents(tenant_id, id),
  FOREIGN KEY (tenant_id, binding_id) REFERENCES agent.notification_bindings(tenant_id, id)
);

CREATE TABLE agent.notification_delivery_nonces (
  nonce text PRIMARY KEY,
  tenant_id uuid NOT NULL,
  intent_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notification_delivery_nonces_expires_idx ON agent.notification_delivery_nonces (expires_at);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['notification_bindings','notification_intents','notification_attempts'] LOOP
    EXECUTE format('ALTER TABLE agent.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON TABLE agent.%I FROM PUBLIC', t);
    EXECUTE format($p$CREATE POLICY api_tenant ON agent.%I TO aiagent_api
      USING (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)
      WITH CHECK (tenant_id = nullif(current_setting('aiagent.tenant_id', true), '')::uuid)$p$, t);
    EXECUTE format($p$CREATE POLICY service_all ON agent.%I TO aiagent_sentinel, aiagent_ops
      USING (true) WITH CHECK (true)$p$, t);
  END LOOP;
END $$;

-- Notification delivery and secrets are restricted to the API control-plane boundary and ops read-only audit;
-- Tool Gateway and Relay never receive SELECT/UPDATE access to notification_bindings secrets or intents.
GRANT SELECT, INSERT, UPDATE ON agent.notification_bindings, agent.notification_intents, agent.notification_attempts
  TO aiagent_api;
GRANT SELECT (
  tenant_id, id, owner_user_id, workspace_id, channel, destination_scheme, destination_host, destination_port,
  binding_version, consent_granted, consent_version, opted_out_at, revoked_at
) ON agent.notification_bindings TO aiagent_sentinel;
GRANT SELECT ON agent.notification_intents TO aiagent_sentinel;
GRANT SELECT ON agent.notification_bindings, agent.notification_intents, agent.notification_attempts TO aiagent_ops;
GRANT UPDATE (dispatched_at, relay_lease_epoch, relay_owner_ref, relay_lease_until, dispatch_attempts, next_attempt_at, last_error_code)
  ON agent.outbox_messages TO aiagent_api;

ALTER TABLE agent.automation_watches
  ADD COLUMN IF NOT EXISTS local_hour smallint NOT NULL DEFAULT 9 CHECK (local_hour BETWEEN 0 AND 23),
  ADD COLUMN IF NOT EXISTS local_minute smallint NOT NULL DEFAULT 0 CHECK (local_minute BETWEEN 0 AND 59);

ALTER TABLE agent.notification_delivery_nonces ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON agent.notification_delivery_nonces FROM PUBLIC;
CREATE POLICY notification_delivery_nonces_sentinel_all ON agent.notification_delivery_nonces
  FOR ALL TO aiagent_sentinel USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, DELETE ON agent.notification_delivery_nonces TO aiagent_sentinel;
