-- PostgreSQL16 real DB fixture; validates *DDL semantics*, not the missing Harness runtime.
BEGIN;
INSERT INTO agent.tenants(id) VALUES ('01900000-0000-7000-8000-000000000001'),('01900000-0000-7000-8000-000000000006');
INSERT INTO agent.users(id) VALUES ('01900000-0000-7000-8000-000000000002'),('01900000-0000-7000-8000-000000000007');
INSERT INTO agent.tenant_memberships(tenant_id,user_id) VALUES
 ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000002'),
 ('01900000-0000-7000-8000-000000000006','01900000-0000-7000-8000-000000000007');
INSERT INTO agent.tasks(tenant_id,id,owner_user_id,status_code,canonical_schema_version,request_json)
VALUES ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000003',
 '01900000-0000-7000-8000-000000000002','QUEUED',1,'{}');
INSERT INTO agent.task_goal_revisions(tenant_id,task_id,revision,goal_ref,goal_sha256)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000003',
 1,'audit-goal',repeat('a',64));
INSERT INTO agent.runs(tenant_id,id,task_id,attempt_no,input_goal_revision,status_code,state_schema_version,lease_epoch)
VALUES ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000004',
 '01900000-0000-7000-8000-000000000003',1,1,'RUNNING',1,1),
 ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000008',
 '01900000-0000-7000-8000-000000000003',2,1,'QUEUED',1,0);
INSERT INTO agent.run_checkpoints
 (tenant_id,id,task_id,run_id,checkpoint_revision,step_no,phase_code,writer_lease_epoch,
  applied_goal_revision,checkpoint_schema_version,checkpoint_json,snapshot_sha256)
VALUES
 ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000010',
  '01900000-0000-7000-8000-000000000003','01900000-0000-7000-8000-000000000004',
  1,0,'MODEL',1,1,1,'{}',repeat('a',64)),
 ('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000011',
  '01900000-0000-7000-8000-000000000003','01900000-0000-7000-8000-000000000004',
  2,0,'INTENT',1,1,1,'{}',repeat('b',64));
UPDATE agent.runs SET current_checkpoint_id='01900000-0000-7000-8000-000000000011'
 WHERE tenant_id='01900000-0000-7000-8000-000000000001'
 AND id='01900000-0000-7000-8000-000000000004';
INSERT INTO agent.watches(tenant_id,id,owner_user_id,status_code,condition_schema_version,condition_json,schedule_json,timezone_id)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000005',
 '01900000-0000-7000-8000-000000000002','active',1,'{}','{}','Asia/Tokyo');
INSERT INTO agent.event_aggregate_counters(tenant_id,aggregate_type,aggregate_id,next_aggregate_sequence)
VALUES('01900000-0000-7000-8000-000000000001','watch','01900000-0000-7000-8000-000000000005',2);
INSERT INTO agent.tenant_event_counters(tenant_id,next_delivery_offset)
VALUES('01900000-0000-7000-8000-000000000001',2);
INSERT INTO agent.event_retention_floors(tenant_id) VALUES ('01900000-0000-7000-8000-000000000001');
INSERT INTO agent.domain_events
 (tenant_id,id,aggregate_type,aggregate_id,aggregate_sequence,delivery_offset,event_kind,
  schema_version,payload_schema_version,payload_json,actor_json,audience_code,owner_user_id,
  task_id,run_id,watch_id,correlation_id,occurred_at)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000012',
 'watch','01900000-0000-7000-8000-000000000005',1,1,'watch.triggered',
 '1.0','1.0','{}','{}','owner_private','01900000-0000-7000-8000-000000000002',
 NULL,NULL,'01900000-0000-7000-8000-000000000005','r4',now());
INSERT INTO agent.outbox_messages(tenant_id,id,topic,dedupe_key,event_id,payload_schema_version,safe_payload_json)
VALUES('01900000-0000-7000-8000-000000000001','01900000-0000-7000-8000-000000000013',
 'domain_event','r4','01900000-0000-7000-8000-000000000012',1,'{}');
COMMIT;

DO $$
BEGIN
 IF (SELECT count(*) FROM agent.run_checkpoints WHERE step_no=0 AND
 run_id='01900000-0000-7000-8000-000000000004') <> 2
 THEN RAISE EXCEPTION 'failed two checkpoints in same step'; END IF;
 IF (SELECT count(*) FROM agent.domain_events WHERE task_id IS NULL AND watch_id IS NOT NULL) <> 1
 THEN RAISE EXCEPTION 'Task-less Watch event failed'; END IF;
 IF (SELECT count(*) FROM agent.outbox_messages WHERE event_id='01900000-0000-7000-8000-000000000012') <> 1
 THEN RAISE EXCEPTION 'event outbox FK failed'; END IF;
 RAISE NOTICE 'PASS core fixture: two checkpoints at same step, Task-less Watch event, Outbox FK';
END $$;

-- Ensure cross-run head reference is impossible, using a subtransaction.
DO $$
DECLARE violated boolean := false;
BEGIN
 BEGIN
  UPDATE agent.runs SET current_checkpoint_id='01900000-0000-7000-8000-000000000011'
   WHERE id='01900000-0000-7000-8000-000000000008';
  SET CONSTRAINTS agent.runs_checkpoint_head_fk IMMEDIATE;
 EXCEPTION WHEN foreign_key_violation THEN
  violated := true;
 END;
 IF NOT violated THEN RAISE EXCEPTION 'cross-run checkpoint head FK not enforced'; END IF;
 RAISE NOTICE 'PASS cross-run checkpoint head FK rejected';
END $$;
-- Tenant B cannot claim Tenant A's owner membership.
DO $$
DECLARE violated boolean := false;
BEGIN
 BEGIN
  INSERT INTO agent.watches(tenant_id,id,owner_user_id,status_code,condition_schema_version,condition_json,schedule_json,timezone_id)
   VALUES('01900000-0000-7000-8000-000000000006','01900000-0000-7000-8000-000000000014',
    '01900000-0000-7000-8000-000000000002','active',1,'{}','{}','Asia/Tokyo');
 EXCEPTION WHEN foreign_key_violation THEN violated := true;
 END;
 IF NOT violated THEN RAISE EXCEPTION 'cross-tenant watch owner FK not enforced'; END IF;
 RAISE NOTICE 'PASS cross-tenant owner FK rejected';
END $$;
