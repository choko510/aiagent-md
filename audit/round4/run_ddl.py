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
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1','-v','migration_checksum_sha256='+hashlib.sha256(v2.encode()).hexdigest()],
  input=v2,text=True,capture_output=True,timeout=90)
 print('V2_MIGRATION_RC',z.returncode,'STDOUT',z.stdout[-1000:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("Forward migration v2 failed")
 fixture_v2=(ROOT/'audit/round4/expansion_integration.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],
  input=fixture_v2,text=True,capture_output=True,timeout=90)
 print('V2_FIXTURE_RC',z.returncode,'STDOUT',z.stdout[-1100:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("External Event/Storefront/Digest/Parser v2 FK fixture failed")
 ingress=subprocess.run([sys.executable,str(ROOT/'audit/round5/run_ingress_concurrency.py')],text=True,capture_output=True,timeout=180)
 print('INGRESS_CONCURRENCY_RC',ingress.returncode)
 print(ingress.stdout[-2800:],ingress.stderr[-2500:])
 if ingress.returncode:raise RuntimeError('Subscription receipt and Task transactional concurrency fixture failed')
 v3=(ROOT/'audit/round4/expand_v3.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1','-v','migration_checksum_sha256='+hashlib.sha256(v3.encode()).hexdigest()],input=v3,text=True,capture_output=True,timeout=90)
 print('V3_MIGRATION_RC',z.returncode,'STDOUT',z.stdout[-1100:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("Forward migration v3 failed")
 fixture_v3=(ROOT/'audit/round4/expansion_v3_integration.sql').read_text()
 z=subprocess.run(['docker','exec','-i',name,'psql','-X','-U','postgres','-d','agent_test','-v','ON_ERROR_STOP=1'],input=fixture_v3,text=True,capture_output=True,timeout=90)
 print('V3_FIXTURE_RC',z.returncode,'STDOUT',z.stdout[-1100:],'STDERR',z.stderr[-2600:])
 if z.returncode:raise RuntimeError("Source/Change/Discovery/Risk/Trend/Feedback v3 fixture failed")
 # Verify migration registry, immutable checksum, replay skip, changed-version rejection.
 for version,contents in [(1,schema),(2,v2),(3,v3)]:
  q=run(['docker','exec',name,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',
    'SELECT checksum_sha256 FROM agent.schema_migrations WHERE version='+str(version)])
  checksum=q.stdout.strip()
  assert q.returncode==0 and checksum==hashlib.sha256(contents.encode()).hexdigest(),f'migration checksum {version} mismatch: {checksum}'
  print('MIGRATION_CHECKSUM_PASS',version,checksum[:14])
 check=run(['docker','exec',name,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',
   "SELECT schema_version FROM agent.schema_control WHERE singleton=true"])
 assert check.returncode==0 and check.stdout.strip()=='3',check.stdout+' '+check.stderr
 print('MIGRATION_VERSION_PASS',check.stdout.strip())
 # Simulation of production runner behavior: existing matching version is a successful SKIP.
 # Different checksum is a fail-closed immutability violation, not a DDL reapply.
 def migration_decision(version,submitted_sha):
  q=run(['docker','exec',name,'psql','-X','-A','-t','-U','postgres','-d','agent_test','-c',
   'SELECT checksum_sha256 FROM agent.schema_migrations WHERE version='+str(version)])
  assert q.returncode==0
  current=q.stdout.strip()
  if current==submitted_sha:return 'skip'
  if current:return 'fail_checksum'
  return 'apply'
 assert migration_decision(2,hashlib.sha256(v2.encode()).hexdigest())=='skip'
 assert migration_decision(3,hashlib.sha256(v3.encode()).hexdigest())=='skip'
 assert migration_decision(3,'0'*64)=='fail_checksum'
 print('MIGRATION_REPLAY_PASS matching version SKIP, altered checksum FAIL')
 print('RESULT PASS: fresh schema executed and basic catalog assertions passed')
finally:
 run(['docker','rm','-f',name])
