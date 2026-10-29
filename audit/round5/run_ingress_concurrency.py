#!/usr/bin/env python3
"""Real PostgreSQL concurrent subscription ingestion transaction fixture.
Not a deployed provider adapter or full DomainEvent/Outbox implementation."""
import subprocess,concurrent.futures,sys
C='round4-spec-postgresql'
T='01900000-0000-7000-8000-000000000001'
U='01900000-0000-7000-8000-000000000002'
S='01900000-0000-7000-8000-000000000301'
def run(sql):
 p=subprocess.run(['docker','exec','-i',C,'psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1',
 '-U','postgres','-d','agent_test'],input=sql,text=True,capture_output=True,timeout=90)
 return p
init=f"""BEGIN;
INSERT INTO agent.external_event_subscriptions(tenant_id,id,owner_user_id,
provider_code,provider_actor_native_id,connector_credential_ref,transport_code,
status_code,filters_schema_version,filters_json,policy_json,consent_ref)
VALUES('{T}','{S}','{U}','x','actor100','credential:test',
'x_timeline_poll','active','1.0','{{}}','{{}}','consent');
INSERT INTO agent.external_subscription_generations(tenant_id,subscription_id,generation,transition_reason)
VALUES('{T}','{S}',1,'started');
COMMIT;"""
p=run(init)
assert p.returncode==0,p.stderr[-1300:]
def sql_for(event_id,rollback=False):
  return f"""BEGIN;
DO $$
DECLARE r uuid; t uuid; g bigint; active text;
BEGIN
 SELECT status_code,generation INTO active,g
 FROM agent.external_event_subscriptions WHERE tenant_id='{T}' AND id='{S}' FOR UPDATE;
 IF active='active' AND g=1 THEN
  INSERT INTO agent.external_event_receipts
  (tenant_id,id,subscription_id,subscription_generation,provider_code,
  native_event_kind,native_event_id,actor_native_id,source_sha256,disposition_code)
  VALUES('{T}',gen_random_uuid(),'{S}',1,'x','post_created','{event_id}','actor100',
  repeat('a',64),'accepted')
  ON CONFLICT(tenant_id,subscription_id,provider_code,native_event_kind,native_event_id)
  DO NOTHING RETURNING id INTO r;
  IF r IS NOT NULL THEN
   INSERT INTO agent.tasks(tenant_id,id,owner_user_id,status_code,canonical_schema_version,request_json)
   VALUES('{T}',gen_random_uuid(),'{U}','QUEUED',1,'{{}}') RETURNING id INTO t;
   INSERT INTO agent.task_goal_revisions (tenant_id,task_id,revision,goal_ref,goal_sha256)
   VALUES('{T}',t,1,'fixture:external-event',repeat('b',64));
   UPDATE agent.external_event_receipts SET task_id=t WHERE tenant_id='{T}' AND id=r;
   {"RAISE EXCEPTION 'injected-after-Task-before-commit';" if rollback else ""}
  END IF;
 END IF;
END $$;
COMMIT;"""
def run_one_delivery(i): 
 p=run(sql_for('post_parallel_1'))
 return i,p.returncode,p.stderr[-550:]
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
 results=list(ex.map(run_one_delivery,range(24)))
fail=[r for r in results if r[1]]
print('INGRESS_PARALLEL_FAILURES',len(fail),fail[:5])
assert not fail,fail[:4]
def value(query):
 p=run(query)
 assert p.returncode==0,p.stderr
 return p.stdout.strip()
a=value(f"""SELECT COUNT(*), COUNT(DISTINCT task_id), COUNT(*) FILTER (WHERE task_id IS NOT NULL)
FROM agent.external_event_receipts WHERE tenant_id='{T}' AND subscription_id='{S}'""")
assert a=='1|1|1',a
print('PASS 24 concurrent duplicate event deliveries yield exactly 1 receipt and 1 linked Task')
# Injected exception inside same transaction cannot leave receipt or Task.
p=run(sql_for('post_crash_before_commit',True))
assert p.returncode!=0,'expected injected failure'
assert value(f"SELECT COUNT(*) FROM agent.external_event_receipts WHERE subscription_id='{S}' AND native_event_id='post_crash_before_commit'")=='0'
print('PASS PostgreSQL rollback leaves no partial receipt when Task creation fails')
p=run(f"""BEGIN;
INSERT INTO agent.external_subscription_generations(tenant_id,subscription_id,generation,transition_reason)
VALUES('{T}','{S}',2,'revoked');
UPDATE agent.external_event_subscriptions SET generation=2,status_code='revoked',record_version=record_version+1
WHERE tenant_id='{T}' AND id='{S}' AND generation=1;
COMMIT;""")
assert p.returncode==0,p.stderr
p=run(sql_for('post_after_revoke'))
assert p.returncode==0,p.stderr
assert value(f"SELECT COUNT(*) FROM agent.external_event_receipts WHERE subscription_id='{S}' AND native_event_id='post_after_revoke'")=='0'
print('PASS revoked generation prevents newly accepted receipt/Task with locked subscription')
print('LIMIT: this is a synthetic SQL implementation, NOT actual connector, signed webhook, Watch, DomainEvent/Outbox or Harness test')
