#!/usr/bin/env python3
"""Independent execution of the proposal in PR #10. NOT Agent Harness runtime tests."""
import hashlib, pathlib, re, subprocess, sys, time, json, os

ROOT=pathlib.Path(__file__).resolve().parents[2]
doc=(ROOT/'docs/10-infrastructure/database-schema-and-migrations.md').read_text()
matches=re.findall(r'```sql\n(.*?)\n```',doc,re.S)
schema=next((t for t in matches if 'CREATE SCHEMA IF NOT EXISTS agent;' in t),None)
assert schema is not None, "DDL fenced block missing"
sql_path=ROOT/'audit/round4/extracted_initial.sql'
sql_path.write_text(schema)
print('DDL_EXTRACT',json.dumps({'bytes':len(schema),'sha256':hashlib.sha256(schema.encode()).hexdigest(),'tables':schema.count('CREATE TABLE agent.')}))
name="round4-spec-postgresql"
def run(args,**kw):
 print('RUN', ' '.join(map(str,args)),flush=True)
 return subprocess.run(args,text=True,capture_output=True,**kw)
try:
 p=run(['docker','run','--detach','--rm','--name',name,'-e','POSTGRES_PASSWORD=audit-only',
        '-e','POSTGRES_DB=agent_test','postgres:16'])
 if p.returncode: raise RuntimeError('docker postgres start: '+p.stderr)
 for i in range(60):
  p=run(['docker','exec',name,'pg_isready','-U','postgres','-d','agent_test'])
  if p.returncode==0: break
  time.sleep(1)
 else: raise RuntimeError('postgres readiness timeout')
 r=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test',
 '-v','ON_ERROR_STOP=1','-v','migration_checksum_sha256='+hashlib.sha256(schema.encode()).hexdigest()],
 input=schema,text=True,capture_output=True)
 print('DDL_PSQL_RC',r.returncode)
 print('DDL_PSQL_STDOUT_LAST',r.stdout[-3000:])
 print('DDL_PSQL_STDERR_LAST',r.stderr[-4000:])
 if r.returncode != 0:
  print('RESULT FAIL: DDL proposal not executable; needs PR #10 correction',file=sys.stderr)
  sys.exit(1)
 q=run(['docker','exec',name,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',
 "SELECT count(*) FROM information_schema.tables WHERE table_schema='agent'"])
 print('PG_AGENT_TABLE_COUNT',q.stdout.strip());assert q.returncode==0
 checks={"domain_events":"SELECT to_regclass('agent.domain_events') IS NOT NULL",
         "run_checkpoints":"SELECT to_regclass('agent.run_checkpoints') IS NOT NULL",
         "generic_counters":"SELECT to_regclass('agent.event_aggregate_counters') IS NOT NULL",
         "tenant_counters":"SELECT to_regclass('agent.tenant_event_counters') IS NOT NULL",
         "retention_floors":"SELECT to_regclass('agent.event_retention_floors') IS NOT NULL",
         "old_task_event_absent":"SELECT to_regclass('agent.task_events') IS NULL"}
 for key,sql in checks.items():
  z=run(['docker','exec',name,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',sql])
  assert z.returncode==0 and z.stdout.strip()=='t',key+': '+z.stdout+' '+z.stderr
  print('ASSERT_PASS',key)
 integration=(ROOT/'audit/round4/contract_integration.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],
  input=integration,text=True,capture_output=True,timeout=90)
 print('CONTRACT_SQL_RC',z.returncode,'STDOUT',z.stdout[-1200:],'STDERR',z.stderr[-2500:])
 if z.returncode:raise RuntimeError("PostgreSQL FK/Checkpoint/Task-less Event contract failed")
 concurrent=subprocess.run([sys.executable,str(ROOT/'audit/round4/run_concurrency.py')],
  text=True,capture_output=True,timeout=180)
 print('CONCURRENCY_RC',concurrent.returncode)
 print(concurrent.stdout[-2000:],concurrent.stderr[-2000:])
 if concurrent.returncode:raise RuntimeError('PostgreSQL multiwriter sequence test failed')
 v2=(ROOT/'audit/round4/expand_v2.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],
  input=v2,text=True,capture_output=True,timeout=90)
 print('V2_MIGRATION_RC',z.returncode,'STDOUT',z.stdout[-1000:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("Forward migration v2 failed")
 fixture_v2=(ROOT/'audit/round4/expansion_integration.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],
  input=fixture_v2,text=True,capture_output=True,timeout=90)
 print('V2_FIXTURE_RC',z.returncode,'STDOUT',z.stdout[-1100:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("External Event/Storefront/Digest/Parser v2 FK fixture failed")
 print('RESULT PASS: fresh schema executed and basic catalog assertions passed')
finally:
 run(['docker','rm','-f',name])
