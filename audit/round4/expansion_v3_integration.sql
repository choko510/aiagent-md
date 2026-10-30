BEGIN;
INSERT INTO agent.evidence_records (tenant_id,id,task_id,artifact_id,source_locator,source_kind_code,fetched_at,
 confidence_code,verification_code,canonical_schema_version,evidence_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000201','01900000-0000-7000-8000-000000000003',
 '01900000-0000-7000-8000-000000000108','https://example.invalid/item','web',
 now(),'source_claim','unverified',1,'{}');
INSERT INTO agent.source_attempt_receipts
(tenant_id,id,owner_user_id,source_key,provider_attempt_key,provider_ref,outcome_code,evidence_id,artifact_id,scope_digest,coverage_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000202','01900000-0000-7000-8000-000000000002',
 'seller-123','fetch001','connector:x','success','01900000-0000-7000-8000-000000000201',
 '01900000-0000-7000-8000-000000000108',repeat('a',64),'complete');
INSERT INTO agent.source_profile_revisions
(tenant_id,id,owner_user_id,source_key,window_start_at,window_end_at,profile_revision,status_code,metrics_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000203','01900000-0000-7000-8000-000000000002','seller-123',
 now()-interval '2 hours',now(),1,'valid','{}');
INSERT INTO agent.change_baselines(tenant_id,id,watch_id,definition_revision,scope_digest,
 field_key,canonical_entity_id,mapping_revision,baseline_evidence_id,source_attempt_id,
 accepted_value_json,accepted_observed_at)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000204','01900000-0000-7000-8000-000000000005',1,
 repeat('a',64),'price.current','01900000-0000-7000-8000-000000000103',1,
 '01900000-0000-7000-8000-000000000201','01900000-0000-7000-8000-000000000202',
 '{"amountMinorUnits":"10000","currency":"JPY"}',now());
INSERT INTO agent.discovery_plans (tenant_id,id,task_id,run_id,applied_goal_revision,status_code,
plan_digest,budget_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000205','01900000-0000-7000-8000-000000000003','01900000-0000-7000-8000-000000000004',1,'active',
 repeat('b',64),'{"maxCalls":20}');
INSERT INTO agent.discovery_slots(tenant_id,id,plan_id,slot_key,state_code)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000206','01900000-0000-7000-8000-000000000205','slot001','completed');
INSERT INTO agent.opportunity_candidates(tenant_id,id,plan_id,candidate_key,canonical_entity_id,
entity_mapping_revision,source_attempt_id,evidence_id,stage_code,reason_codes_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000207',
 '01900000-0000-7000-8000-000000000205','item001','01900000-0000-7000-8000-000000000103',1,
 '01900000-0000-7000-8000-000000000202','01900000-0000-7000-8000-000000000201',
 'needs_risk','[]');
INSERT INTO agent.risk_decision_receipts(tenant_id,id,candidate_id,engine_version,input_digest,
 expected_profit_minor_units,currency_code,uncertainty_code,scenario_json,evidence_refs_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000208',
 '01900000-0000-7000-8000-000000000207','1.0',repeat('c',64),NULL,NULL,'unknown','{}','[]');
INSERT INTO agent.trend_receipts(tenant_id,id,owner_user_id,canonical_entity_id,mapping_revision,
 metric_key,bucket_start_at,bucket_end_at,score_bps,sample_count,independence_code,
 coverage_code,input_observation_refs_json,source_profile_refs_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000209','01900000-0000-7000-8000-000000000002','01900000-0000-7000-8000-000000000103',1,
 'price',now()-interval '1 hour',now(),NULL,1,'unknown','partial','[]','[]');
INSERT INTO agent.memory_feedback_mutations (tenant_id,id,owner_user_id,idempotency_key,target_entity_id,
 target_mapping_revision,scope_code,user_consent_ref,action_code,preference_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000210','01900000-0000-7000-8000-000000000002',
 'exclude-seller-123','01900000-0000-7000-8000-000000000103',1,'user','consent-feedback','activate','{"exclude":true}');
COMMIT;
DO $$
DECLARE duplicate_found boolean:=false; bad_tenant_found boolean:=false;
BEGIN
 BEGIN
 INSERT INTO agent.discovery_slots(tenant_id,id,plan_id,slot_key,state_code)
 VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000211',
 '01900000-0000-7000-8000-000000000205','slot001','completed');
 EXCEPTION WHEN unique_violation THEN duplicate_found:=true; END;
 BEGIN
 INSERT INTO agent.memory_feedback_mutations(tenant_id,id,owner_user_id,idempotency_key,target_entity_id,
 target_mapping_revision,scope_code,user_consent_ref,action_code,preference_json)
 VALUES('01900000-0000-7000-8000-000000000006','01900000-0000-7000-8000-000000000212',
 '01900000-0000-7000-8000-000000000002','test-other-tenant','01900000-0000-7000-8000-000000000103',1,'user','consent','activate','{}');
 EXCEPTION WHEN foreign_key_violation THEN bad_tenant_found:=true; END;
 IF NOT (duplicate_found AND bad_tenant_found) THEN RAISE EXCEPTION 'v3 constraints failed: %, %', duplicate_found,bad_tenant_found; END IF;
 IF (SELECT count(*) FROM agent.risk_decision_receipts WHERE expected_profit_minor_units IS NULL AND uncertainty_code='unknown')<>1
 THEN RAISE EXCEPTION 'unknown profit not preserved'; END IF;
 RAISE NOTICE 'PASS R4 v3 FK/slot unique/feedback isolation and unknown money';
END $$;
