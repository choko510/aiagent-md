BEGIN;
-- One durable subscription bound to a stable actor identity and explicit owner.
CREATE TABLE agent.external_event_subscriptions (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 provider_code text NOT NULL, provider_actor_native_id text NOT NULL,
 connector_credential_ref text NOT NULL, transport_code text NOT NULL,
 generation bigint NOT NULL DEFAULT 1 CHECK(generation BETWEEN 1 AND 9223372036854775807),
 record_version bigint NOT NULL DEFAULT 1 CHECK(record_version BETWEEN 1 AND 9223372036854775807),
 status_code text NOT NULL CHECK(status_code IN ('draft','active','paused','blocked','revoked')),
 filters_schema_version text NOT NULL, filters_json jsonb NOT NULL CHECK(jsonb_typeof(filters_json)='object'),
 policy_json jsonb NOT NULL CHECK(jsonb_typeof(policy_json)='object'),
 consent_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,id,generation),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id)
);
-- Immutable generation records keep old receipts valid when current subscription generation increments.
CREATE TABLE agent.external_subscription_generations (
 tenant_id uuid NOT NULL, subscription_id uuid NOT NULL,
 generation bigint NOT NULL CHECK(generation BETWEEN 1 AND 9223372036854775807),
 created_at timestamptz NOT NULL DEFAULT now(), transition_reason text NOT NULL,
 PRIMARY KEY(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,subscription_id)
 REFERENCES agent.external_event_subscriptions(tenant_id,id)
);
CREATE TABLE agent.external_event_cursors (
 tenant_id uuid NOT NULL, subscription_id uuid NOT NULL,
 generation bigint NOT NULL,
 since_native_id text, next_token_ref text, coverage_code text NOT NULL,
 last_complete_poll_at timestamptz, gap_start_at timestamptz,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,subscription_id,generation)
 REFERENCES agent.external_subscription_generations(tenant_id,subscription_id,generation)
);
CREATE TABLE agent.external_event_receipts (
 tenant_id uuid NOT NULL, id uuid NOT NULL, subscription_id uuid NOT NULL,
 subscription_generation bigint NOT NULL,
 provider_code text NOT NULL, native_event_kind text NOT NULL,
 native_event_id text NOT NULL, actor_native_id text NOT NULL,
 source_created_at timestamptz, received_at timestamptz NOT NULL DEFAULT now(),
 source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 payload_artifact_id uuid, disposition_code text NOT NULL CHECK
 (disposition_code IN ('accepted','duplicate','ignored','blocked','quarantined','unknown')),
 task_id uuid, domain_event_id uuid,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,subscription_id,provider_code,native_event_kind,native_event_id),
 FOREIGN KEY(tenant_id,subscription_id,subscription_generation)
 REFERENCES agent.external_subscription_generations(tenant_id,subscription_id,generation),
 FOREIGN KEY(tenant_id,payload_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,task_id) REFERENCES agent.tasks(tenant_id,id),
 FOREIGN KEY(tenant_id,domain_event_id) REFERENCES agent.domain_events(tenant_id,id)
);
CREATE INDEX external_receipts_by_subscription ON agent.external_event_receipts
 (tenant_id,subscription_id,received_at DESC);
-- Storefront: physical seller/account/branch, distinct from corporate organization.
CREATE TABLE agent.canonical_entities (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 entity_kind text NOT NULL CHECK(entity_kind IN
 ('product','hotel_property','hotel_rate','organization','retailer_storefront','public_person')),
 mapping_revision bigint NOT NULL DEFAULT 1 CHECK(mapping_revision>0),
 status_code text NOT NULL CHECK(status_code IN ('active','review','split','retired')),
 attributes_json jsonb NOT NULL CHECK(jsonb_typeof(attributes_json)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id)
);
CREATE TABLE agent.entity_source_links (
 tenant_id uuid NOT NULL, id uuid NOT NULL, canonical_entity_id uuid NOT NULL,
 source_id text NOT NULL, source_listing_id text NOT NULL, extraction_revision text NOT NULL,
 relation_code text NOT NULL CHECK(relation_code IN ('accepted','rejected','review')),
 mapping_revision bigint NOT NULL CHECK(mapping_revision>0),
 evidence_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
 invalidated_at timestamptz,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,source_id,source_listing_id,extraction_revision,mapping_revision),
 FOREIGN KEY(tenant_id,canonical_entity_id) REFERENCES agent.canonical_entities(tenant_id,id),
 FOREIGN KEY(tenant_id,evidence_id) REFERENCES agent.evidence_records(tenant_id,id)
);
CREATE INDEX entity_links_active ON agent.entity_source_links
 (tenant_id,canonical_entity_id,mapping_revision) WHERE invalidated_at IS NULL;
-- Ranker never sends: only the canonical CHAT_07 delivery sender sends a digest.
CREATE TABLE agent.notification_digest_envelopes (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 destination_ref text NOT NULL, policy_revision text NOT NULL,
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 send_state_code text NOT NULL CHECK(send_state_code IN
 ('draft','reserved','queued','sending','provider_accepted','unknown','failed','cancelled')),
 outbox_id uuid, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,outbox_id) REFERENCES agent.outbox_messages(tenant_id,id)
);
CREATE TABLE agent.notification_digest_members (
 tenant_id uuid NOT NULL, digest_id uuid NOT NULL, notification_id uuid NOT NULL,
 member_state_code text NOT NULL CHECK(member_state_code IN
 ('reserved','covered_accepted','coverage_unknown','released','blocked')),
 rank_decision_ref text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,digest_id,notification_id),
 FOREIGN KEY(tenant_id,digest_id) REFERENCES agent.notification_digest_envelopes(tenant_id,id),
 FOREIGN KEY(tenant_id,notification_id) REFERENCES agent.notification_records(tenant_id,id)
);
-- A cancelled/proven unsent membership is historical, not a permanent lock.
-- Accepted/UNKNOWN digest coverage remains locked against duplicate individual sends.
CREATE UNIQUE INDEX digest_active_member_only ON agent.notification_digest_members
 (tenant_id,notification_id)
 WHERE member_state_code IN ('reserved','covered_accepted','coverage_unknown');
-- Source bytes and derived artifacts must retain provenance and bounded parser budgets.
CREATE TABLE agent.parser_jobs (
 tenant_id uuid NOT NULL, id uuid NOT NULL, owner_user_id uuid NOT NULL,
 source_artifact_id uuid NOT NULL, derived_artifact_id uuid,
 parser_build_digest text NOT NULL, job_state_code text NOT NULL CHECK
 (job_state_code IN ('queued','running','partial','verified','quarantined','failed','cancelled')),
 lease_epoch bigint NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
 job_revision bigint NOT NULL DEFAULT 1 CHECK(job_revision>0),
 budget_json jsonb NOT NULL CHECK(jsonb_typeof(budget_json)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner_user_id) REFERENCES agent.tenant_memberships(tenant_id,user_id),
 FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,derived_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 CHECK(derived_artifact_id IS NULL OR derived_artifact_id <> source_artifact_id)
);
CREATE TABLE agent.artifact_derivations (
 tenant_id uuid NOT NULL, id uuid NOT NULL,
 source_artifact_id uuid NOT NULL, derived_artifact_id uuid NOT NULL,
 parser_job_id uuid NOT NULL, transform_version text NOT NULL,
 locator_schema_version text NOT NULL, locator_json jsonb NOT NULL CHECK(jsonb_typeof(locator_json)='object'),
 source_sha256 char(64) NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 derived_sha256 char(64) NOT NULL CHECK(derived_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,source_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,derived_artifact_id) REFERENCES agent.artifacts(tenant_id,id),
 FOREIGN KEY(tenant_id,parser_job_id) REFERENCES agent.parser_jobs(tenant_id,id),
 UNIQUE(tenant_id,parser_job_id,source_artifact_id,derived_artifact_id)
);
-- Runner computes SHA256 over THIS versioned SQL file and supplies the bound checksum.
-- A repeat launch must compare existing checksum and SKIP rather than rerun CREATE TABLE.
INSERT INTO agent.schema_migrations
 (version,checksum_sha256,name,phase,app_min_reader,app_min_writer)
 VALUES (2,:'migration_checksum_sha256','0002_expansion_ingress_entity_digest_parser','transactional',1,1);
UPDATE agent.schema_control
 SET schema_version=2,changed_at=now()
 WHERE singleton=true AND schema_version=1;
-- Changed 0 rows => abort transaction, never record a false successful migration.
COMMIT;
