#!/usr/bin/env python3
"""Actual PostgreSQL 16 multi-process writers for per-aggregate and tenant offsets."""
import concurrent.futures, subprocess,sys
C='round4-spec-postgresql'
T='01900000-0000-7000-8000-000000000001'
U='01900000-0000-7000-8000-000000000002'
W='01900000-0000-7000-8000-000000000005'
# The durable write path is SQL transaction under test; no Harness workers or Tool calls exist.
SQL=f"""BEGIN;
DO $$
DECLARE seq bigint; off bigint;
BEGIN
UPDATE agent.event_aggregate_counters SET next_aggregate_sequence=next_aggregate_sequence+1
 WHERE tenant_id='{T}' AND aggregate_type='watch' AND aggregate_id='{W}'
 RETURNING next_aggregate_sequence-1 INTO seq;
UPDATE agent.tenant_event_counters SET next_delivery_offset=next_delivery_offset+1
 WHERE tenant_id='{T}' RETURNING next_delivery_offset-1 INTO off;
INSERT INTO agent.domain_events(tenant_id,id,aggregate_type,aggregate_id,aggregate_sequence,
delivery_offset,event_kind,schema_version,payload_schema_version,payload_json,actor_json,
audience_code,owner_user_id,watch_id,correlation_id,occurred_at)
VALUES('{T}',gen_random_uuid(),'watch','{W}',seq,off,'watch.state_changed','1.0','1.0','{{}}',
'{{}}','owner_private','{U}','{W}','concurrent-fixture',now());
END $$;
COMMIT;"""
def one(i):
 p=subprocess.run(['docker','exec','-i',C,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],
 input=SQL,text=True,capture_output=True,timeout=50)
 if p.returncode: return i,p.stderr[-1500:]
 return i,None
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 allresults=list(pool.map(one,range(32)))
failures=[x for x in allresults if x[1]]
print('PARALLEL_WORKER_FAILURES',len(failures),failures[:6])
if failures:sys.exit(1)
q="""SELECT COUNT(*),COUNT(DISTINCT delivery_offset),COUNT(DISTINCT aggregate_sequence),
MIN(delivery_offset),MAX(delivery_offset),MIN(aggregate_sequence),MAX(aggregate_sequence)
FROM agent.domain_events WHERE tenant_id='%s' AND aggregate_type='watch' AND aggregate_id='%s'"""%(T,W)
p=subprocess.run(['docker','exec',C,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',q],
 capture_output=True,text=True)
print('SERIALIZATION_COUNTS',p.stdout.strip(),p.stderr[-300:])
assert p.returncode==0
parts=list(map(int,p.stdout.strip().split('|')))
assert parts==[33,33,33,1,33,1,33],parts
print('PASS 32 parallel postgres writers: unique gapless delivery and aggregate sequences')
