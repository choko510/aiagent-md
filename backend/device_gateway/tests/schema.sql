-- Test fixture only. This is not a numbered production migration.
CREATE SCHEMA device_gateway;
CREATE TABLE device_gateway.pairing_requests (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  owner_id text NOT NULL,
  identity_id uuid NOT NULL,
  device_id uuid NOT NULL,
  origin text NOT NULL,
  token_digest bytea NOT NULL CHECK (octet_length(token_digest) = 32),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 32),
  issued_ms bigint NOT NULL,
  expires_ms bigint NOT NULL,
  state text NOT NULL CHECK (state IN ('WAITING_DEVICE','WAITING_APPROVAL','APPROVED','CONSUMED','LOCKED')),
  failures integer NOT NULL DEFAULT 0 CHECK (failures BETWEEN 0 AND 5),
  public_key bytea CHECK (public_key IS NULL OR octet_length(public_key) = 32),
  binding_digest bytea CHECK (binding_digest IS NULL OR octet_length(binding_digest) = 32),
  code_digest bytea CHECK (code_digest IS NULL OR octet_length(code_digest) = 32)
);
CREATE TABLE device_gateway.device_identities (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  owner_id text NOT NULL,
  device_id uuid NOT NULL UNIQUE,
  origin text NOT NULL,
  public_key bytea NOT NULL CHECK (octet_length(public_key) = 32),
  binding_digest bytea NOT NULL CHECK (octet_length(binding_digest) = 32),
  revoked boolean NOT NULL DEFAULT false,
  revocation_generation bigint NOT NULL DEFAULT 0 CHECK (revocation_generation >= 0),
  UNIQUE (tenant_id, owner_id, id)
);
ALTER TABLE device_gateway.pairing_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_gateway.pairing_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_pairing ON device_gateway.pairing_requests
  USING (tenant_id = current_setting('aiagent.tenant_id', true) AND owner_id = current_setting('aiagent.owner_id', true))
  WITH CHECK (tenant_id = current_setting('aiagent.tenant_id', true) AND owner_id = current_setting('aiagent.owner_id', true));
ALTER TABLE device_gateway.device_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_gateway.device_identities FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_identity ON device_gateway.device_identities
  USING (tenant_id = current_setting('aiagent.tenant_id', true) AND owner_id = current_setting('aiagent.owner_id', true))
  WITH CHECK (tenant_id = current_setting('aiagent.tenant_id', true) AND owner_id = current_setting('aiagent.owner_id', true));
