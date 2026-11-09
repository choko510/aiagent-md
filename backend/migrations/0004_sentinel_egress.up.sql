-- 0004_sentinel_egress: Sentinel permits and egress transport receipts (09 sentinel-authority, network-egress-and-ssrf).
-- A permit binds ONE tool intent to ONE destination (scheme/host/port/method) for a short time. The Trusted Egress
-- re-checks the permit, the intent/approval/run state and the resolved address at send time and records a receipt.
CREATE TABLE agent.egress_permits (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  tool_intent_id uuid NOT NULL,
  run_id uuid NOT NULL,
  run_lease_epoch bigint NOT NULL CHECK (run_lease_epoch >= 1),
  scheme text NOT NULL CHECK (scheme IN ('https', 'http')),
  host text NOT NULL CHECK (host ~ '^[a-z0-9.-]{1,253}$'),
  port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  method text NOT NULL CHECK (method IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'CONNECT')),
  credential_recipient text,
  max_uses integer NOT NULL DEFAULT 1 CHECK (max_uses BETWEEN 1 AND 100),
  uses integer NOT NULL DEFAULT 0 CHECK (uses >= 0),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, tool_intent_id) REFERENCES agent.tool_intents(tenant_id, id),
  CHECK (uses <= max_uses)
);
CREATE INDEX egress_permits_by_intent ON agent.egress_permits (tenant_id, tool_intent_id);

CREATE TABLE agent.egress_receipts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid,
  permit_id uuid,
  decision text NOT NULL CHECK (decision IN ('ALLOWED', 'DENIED')),
  reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]{1,64}$'),
  host text,
  port integer,
  resolved_ip inet,
  bytes_up bigint,
  bytes_down bigint,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX egress_receipts_recent ON agent.egress_receipts (recorded_at DESC);
