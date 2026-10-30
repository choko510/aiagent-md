#!/usr/bin/env python3
"""Round7 combined specs + exact 61 source PR blob verification."""
import pathlib,re,json,sys,subprocess,collections,os,urllib.request,time
ROOT=pathlib.Path(__file__).resolve().parents[2]
text=(ROOT/"docs/00-foundation/spec-index.md").read_text()
required=re.findall(r"\|\s*\d{2}-\d{2}\s*\|\s*"+chr(96)+r"(docs/[0-9]{2}-[^"+chr(96)+r"]+\.md)"+chr(96),text)
found_all=sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/"docs").glob("[0-9][0-9]-*/*.md"))
found=sorted(set(found_all)&set(required))
baseline_extras=sorted(set(found_all)-set(required))
err=[];broken=[];numeric=[]
if len(baseline_extras)!=10 or any(not x.startswith("docs/00-foundation/") for x in baseline_extras):
 err.append("unexpected pre-existing documents outside 85 module inventory: "+str(baseline_extras))
if len(required)!=85 or len(set(required))!=85 or set(required)!=set(found):err.append(f"inventory wrong {len(required)}, found {len(found)}, missing={set(required)-set(found)}")
counts=dict(collections.Counter(p.split("/")[1][:2] for p in found))
expected={"00":5,"01":6,"02":12,"03":6,"04":9,"05":5,"06":5,"07":5,"08":6,"09":6,"10":5,"11":4,"12":5,"13":3,"14":2,"15":1}
if counts!=expected:err.append(f"owner counts mismatch: {counts}")
for filename in found:
 p=ROOT/filename;body=p.read_text()
 if len(body)<600:err.append("empty/thin: "+filename)
 for b in re.finditer(r"(?:~~~|\x60\x60\x60)json\s*\n(.*?)(?:~~~|\x60\x60\x60)",body,re.S|re.I):
  for field in ("recordVersion","definitionRevision","goalRevision","leaseEpoch","aggregateSequence","deliveryOffset","checkpointRevision"):
   if re.search('"'+field+r'"\s*:\s*-?\d+\s*[,}]',b.group(1)):numeric.append(filename+":"+field)
 for m in re.finditer(r"\[[^]\n]+\]\(([^)\n]+)\)",body):
  url=m.group(1).strip().split()[0].strip("<>").split("#")[0].split("?")[0]
  if not url or url.startswith(("https:","http:","mailto:","data:","sandbox:")):continue
  target=ROOT/url if url.startswith(("docs/","prompts/")) else p.parent/url
  if not target.exists():broken.append({"file":filename,"target":url})
# Cross-owner fingerprint gates on the actual combined files, not merely the source PRs.
obligations={
"docs/10-infrastructure/database-schema-and-migrations.md":["external_subscription_generations","notification_intent_ownership","digest_active_member_only","parser_jobs","change_baselines","memory_feedback_mutations","schema_migrations"],
"docs/13-event-ingestion/event-driven-agent.md":["subscriptionGeneration","nativeEventId","receipt","revoke"],
"docs/13-event-ingestion/notification-intelligence.md":["notification_intent_ownership","coverage_unknown","CHAT_07"],
"docs/07-automation/notification-outbox.md":["notification_intent_ownership","coverage_unknown","UNKNOWN"],
"docs/12-intelligence/entity-resolution.md":["retailer_storefront","mappingRevision","REVIEW"],
"docs/14-adaptive-experience/feedback-learning.md":["retailer_storefront","Memory Writer","Undo"],
"docs/14-adaptive-experience/learned-browser-actions.md":["read_only_click","read_only_fill","POST"],
"docs/01-contracts/evidence-artifact-contracts.md":["TransformLocatorV1","sourceSha256","CitationLocator"],
"docs/15-multimodal/multimodal-ingestion.md":["parser_jobs","sourceSha256","no-network"],
"docs/13-event-ingestion/change-intelligence.md":["change_baselines","scope_digest","unknown"],
"docs/11-qa/end-to-end-acceptance.md":["QA-R5-T01","QA-R5-T02","DomainEvent"]}
for f,terms in obligations.items():
 content=(ROOT/f).read_text()
 absent=[q for q in terms if q not in content]
 if absent:err.append("cross-owner contract markers "+f+": "+str(absent))
# Ensure PostgreSQL smoke tests execute the SQL currently authored in the merged
# CHAT_10 specification, not an old audit snapshot of migration 0002/0003.
db_text=(ROOT/"docs/10-infrastructure/database-schema-and-migrations.md").read_text()
sql_blocks=re.findall(r"(?:`{3}|~~~)sql\s*\n(.*?)(?:`{3}|~~~)",db_text,re.S|re.I)
for version,fragment,fixture in (
 (2,"CREATE TABLE agent.external_event_subscriptions","audit/round4/expand_v2.sql"),
 (3,"CREATE TABLE agent.source_attempt_receipts","audit/round4/expand_v3.sql"),
):
 matching=[x for x in sql_blocks if fragment in x]
 if len(matching)!=1:err.append(f"missing/ambiguous SQL v{version} in canonical DB spec")
 else:
  fixture_bytes=(ROOT/fixture).read_text().strip()
  if matching[0].strip()!=fixture_bytes:
   err.append(f"tested SQL fixture v{version} diverges from canonical PR #10 spec; refresh audit fixture and checksum tests")
if numeric:err.append("JSON int64 numeric "+str(numeric))
print("ROUND7_RELATIVE_LINK_WARNINGS",json.dumps(broken[:50],ensure_ascii=False))
# Relative links are collected for targeted correction; external providers may be missing locally.
manifest=json.loads((ROOT/".premerge/pinned-prs.json").read_text())
if len(manifest["prs"])!=61 or manifest["status"]!="UNAPPROVED_REVIEW_CANDIDATE":err.append("unsafe or wrong manifest")
checked=0
if os.getenv("VERIFY_SOURCE_HEADS","0")=="1":
 token=os.environ["GH_TOKEN"]
 def get(url):
  hdr={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json","User-Agent":"round7-integrated-provenance"}
  for k in range(4):
   try:
    with urllib.request.urlopen(urllib.request.Request(url,headers=hdr),timeout=40) as resp:return json.loads(resp.read())
   except Exception:
    if k==3:raise
    time.sleep(2**k)
 seen=set();repo="choko510/aiagent-md"
 for item in manifest["prs"]:
  n=item["number"];p=get(f"https://api.github.com/repos/{repo}/pulls/{n}")
  if p["head"]["sha"]!=item["headSha"] or p["state"]!="open":err.append(f"source #{n} moved")
  files=get(f"https://api.github.com/repos/{repo}/pulls/{n}/files?per_page=100")
  for entry in files:
   path=entry["filename"]
   if not path.startswith("docs/") or not path.endswith(".md"):continue
   if path in seen:err.append("duplicate source path "+path)
   seen.add(path)
   q=ROOT/path
   if not q.exists():err.append("missing source blob "+path);continue
   h=subprocess.check_output(["git","hash-object",str(q)],text=True).strip()
   if h!=entry["sha"]:err.append("unexpected blob for "+path)
   checked+=1
 if checked!=85:err.append(f"expected 85 source blobs: {checked}")
print("ROUND7_INTEGRATED_STATUS",json.dumps({"required":len(required),"localDocs":len(found),"sourceBlobs":checked,
"preexistingFoundation":len(baseline_extras),"crossOwnerContractMarkers":len(obligations),"ddlSourceBlocksChecked":2,"relativeLinkWarnings":len(broken),"int64JsonErrors":len(numeric),"errors":err},ensure_ascii=False))
sys.exit(1 if err else 0)
