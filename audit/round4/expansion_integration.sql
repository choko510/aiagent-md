-- Round 4 synthetic positive/negative FK/unique fixtures on the proposed forward migration.
BEGIN;
INSERT INTO agent.external_event_subscriptions
 (tenant_id,id,owner_user_id,provider_code,provider_actor_native_id,
 connector_credential_ref,transport_code,status_code,filters_schema_version,
 filters_json,policy_json,consent_ref)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000101',
 '01900000-0000-7000-8000-000000000002','x','111111','credential-ref','x_timeline_poll',
 'active','1.0','{}','{}','consent-ref');
INSERT INTO agent.external_subscription_generations
 (tenant_id,subscription_id,generation,transition_reason)
VALUES('01900000-0000-7000-8000-000000000001',
 '01900000-0000-7000-8000-000000000101',1,'initial_subscription');
INSERT INTO agent.external_event_cursors (tenant_id,subscription_id,generation,coverage_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000101',1,'complete');
INSERT INTO agent.external_event_receipts
 (tenant_id,id,subscription_id,subscription_generation,provider_code,native_event_kind,
 native_event_id,actor_native_id,source_sha256,disposition_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000102',
 '01900000-0000-7000-8000-000000000101',1,'x','post_created','1234','111111',
 repeat('a',64),'accepted');
INSERT INTO agent.canonical_entities(tenant_id,id,owner_user_id,entity_kind,status_code,attributes_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000103',
 '01900000-0000-7000-8000-000000000002','retailer_storefront','active','{}'),
('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000104',
 '01900000-0000-7000-8000-000000000002','organization','active','{}');
INSERT INTO agent.entity_source_links(tenant_id,id,canonical_entity_id,source_id,
 source_listing_id,extraction_revision,relation_code,mapping_revision)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000105',
 '01900000-0000-7000-8000-000000000103','marketplace','merchant-123','r1','accepted',1);
INSERT INTO agent.notification_records(tenant_id,id,watch_id,notification_key,destination_ref,delivery_status_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000106',
 '01900000-0000-7000-8000-000000000005','notification-1','binding-1','queued');
INSERT INTO agent.notification_digest_envelopes(tenant_id,id,owner_user_id,
 destination_ref,policy_revision,send_state_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000107',
 '01900000-0000-7000-8000-000000000002','binding-1','1','reserved');
INSERT INTO agent.notification_digest_members(tenant_id,digest_id,notification_id,
 member_state_code,rank_decision_ref)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000107',
 '01900000-0000-7000-8000-000000000106','reserved','decision-1');
INSERT INTO agent.artifacts(tenant_id,id,object_key,sha256_hex,bytes,media_type,
 classification_code,object_state_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000108',
 'storage/input',repeat('c',64),512,'application/pdf','restricted','verified'),
('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000109',
 'storage/derived',repeat('d',64),300,'text/plain','restricted','verified');
INSERT INTO agent.parser_jobs(tenant_id,id,owner_user_id,source_artifact_id,
 derived_artifact_id,parser_build_digest,job_state_code,budget_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000110',
 '01900000-0000-7000-8000-000000000002','01900000-0000-7000-8000-000000000108',
 '01900000-0000-7000-8000-000000000109','sha256:fixture','verified','{}');
INSERT INTO agent.artifact_derivations(tenant_id,id,source_artifact_id,
 derived_artifact_id,parser_job_id,transform_version,locator_schema_version,locator_json,
 source_sha256,derived_sha256)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000111',
 '01900000-0000-7000-8000-000000000108','01900000-0000-7000-8000-000000000109',
 '01900000-0000-7000-8000-000000000110','1.0','1.0',
 '{"type":"pdf","page":1}',repeat('c',64),repeat('d',64));
COMMIT;
DO $$
DECLARE got_unique boolean := false; got_fk boolean := false; got_member_unique boolean := false;
BEGIN
 BEGIN
 INSERT INTO agent.external_event_receipts
 (tenant_id,id,subscription_id,subscription_generation,provider_code,native_event_kind,
 native_event_id,actor_native_id,source_sha256,disposition_code)
 VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000112',
 '01900000-0000-7000-8000-000000000101',1,'x','post_created','1234','111111',
 repeat('b',64),'accepted');
 EXCEPTION WHEN unique_violation THEN got_unique:=true; END;
 BEGIN
 INSERT INTO agent.external_event_subscriptions
 (tenant_id,id,owner_user_id,provider_code,provider_actor_native_id,connector_credential_ref,
 transport_code,status_code,filters_schema_version,filters_json,policy_json,consent_ref)
 VALUES('01900000-0000-7000-8000-000000000006','01900000-0000-7000-8000-000000000113',
 '01900000-0000-7000-8000-000000000002','x','222','cred','x_timeline_poll','active','1.0','{}','{}','consent');
 EXCEPTION WHEN foreign_key_violation THEN got_fk:=true; END;
 BEGIN
 INSERT INTO agent.notification_digest_members(tenant_id,digest_id,notification_id,member_state_code,rank_decision_ref)
 VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000107',
 '01900000-0000-7000-8000-000000000106','reserved','decision-dup');
 EXCEPTION WHEN unique_violation THEN got_member_unique:=true; END;
 IF NOT (got_unique AND got_fk AND got_member_unique)
 THEN RAISE EXCEPTION 'R4 negative FK/dedupe failure: %,%,%',got_unique,got_fk,got_member_unique; END IF;
 IF (SELECT count(*) FROM agent.parser_jobs WHERE job_state_code='verified')<>1
 THEN RAISE EXCEPTION 'missing parser fixture'; END IF;
 RAISE NOTICE 'PASS R4 migration positive/negative FKs and uniqueness';
END $$;

-- Migration validation of historical receipt FK across new generation and status change.
BEGIN;
INSERT INTO agent.external_subscription_generations
 (tenant_id,subscription_id,generation,transition_reason)
VALUES('01900000-0000-7000-8000-000000000001',
 '01900000-0000-7000-8000-000000000101',2,'revoked');
UPDATE agent.external_event_subscriptions SET generation=2,status_code='revoked',
 record_version=record_version+1
WHERE tenant_id='01900000-0000-7000-8000-000000000001'
 AND id='01900000-0000-7000-8000-000000000101' AND generation=1;
COMMIT;
DO $$
BEGIN
 IF (SELECT count(*) FROM agent.external_event_receipts r
 JOIN agent.external_subscription_generations g
 ON r.tenant_id=g.tenant_id AND r.subscription_id=g.subscription_id
 AND r.subscription_generation=g.generation
 WHERE r.subscription_generation=1)<>1
 THEN RAISE EXCEPTION 'lost old generation receipt after revoke'; END IF;
 IF (SELECT count(*) FROM agent.external_event_subscriptions WHERE generation=2 AND status_code='revoked')<>1
 THEN RAISE EXCEPTION 'new generation not activated/revoked'; END IF;
 RAISE NOTICE 'PASS generation advance retains old receipt and current revoked generation';
END $$;
