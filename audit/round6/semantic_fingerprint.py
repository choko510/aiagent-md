#!/usr/bin/env python3
"""Contract semantic-fingerprint check across 61 live Draft PR heads.
An evidence-producing heuristic, not a substitute for independent review."""
import os,urllib.request,urllib.parse,base64,re,json,hashlib,collections,pathlib,time,sys
repo="choko510/aiagent-md"
root=f"https://api.github.com/repos/{repo}"
token=os.environ.get("GH_TOKEN","")
def get(url):
 h={"Accept":"application/vnd.github+json","User-Agent":"spec-contract-semantic-v1"}
 if token:h["Authorization"]="Bearer "+token
 for retry in range(4):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=45) as resp:return json.loads(resp.read())
  except Exception:
   if retry==3:raise
   time.sleep(2**retry)
prs=[p for p in get(root+"/pulls?state=all&per_page=100")
 if re.fullmatch(r"spec/chat-(?:0[0-9]|1[0-5])-.+",p["head"]["ref"])]
assert len(prs)==61
docs={}
for p in prs:
 files=get(f'{root}/pulls/{p["number"]}/files?per_page=100')
 for file in files:
  path=file["filename"]
  if not path.startswith("docs/") or not path.endswith(".md"):continue
  raw=get(f'{root}/contents/{path}?ref={p["head"]["sha"]}')
  s=base64.b64decode(raw["content"]).decode()
  assert path not in docs,path
  docs[path]={"pr":p["number"],"sha":p["head"]["sha"],"body":s}
assert len(docs)==85
numeric=[]
for path,doc in docs.items():
 text=doc["body"]
 # Only JSON fences, not code pseudocode or historical prose.
 for m in re.finditer(r"(?:```|~~~)json\s*\n(.*?)(?:```|~~~)",text,re.S|re.I):
  raw=m.group(1)
  for key in ("recordVersion","definitionRevision","goalRevision","leaseEpoch","aggregateSequence","deliveryOffset","checkpointRevision","expectedRecordVersion"):
   for q in re.finditer(r'"'+key+r'"\s*:\s*(-?\d+)(?![\w.])',raw):
    numeric.append({"file":path,"pr":doc["pr"],"key":key,"value":q.group(1),
      "line":text[:m.start(1)+q.start()].count("\n")+1})
# Lines that claim the *current* canonical DB uses v4/revision0 are critical.
stale=[]
pattern=re.compile(r"(?:DBの|DB |physical|現在).*?(?:gen_random_uuid\(\)|revision\s*(?:default)?\s*0|workspace_idのみ)|"
r"(?:gen_random_uuid\(\)|revision\s*(?:default)?\s*0).*?(?:未解決|P0|矛盾|must map)",re.I)
historic=re.compile(r"Round\s*[123]|旧|history|historical|supersed|解消済み|修正済み|旧DDL|已",re.I)
for path,doc in docs.items():
 for n,line in enumerate(doc["body"].splitlines(),1):
  if pattern.search(line) and not historic.search(line):
   stale.append({"file":path,"pr":doc["pr"],"line":n,"sample":line[:300]})
# Strict cross-layer summaries. Test that canonical artifact is explicitly present.
layers={
 "canon-core":("docs/01-contracts/core-domain-models.md",["RecordVersion","UUIDv7","tenantId","ownerUserId"]),
 "canon-event":("docs/01-contracts/event-envelope-and-stream.md",["DomainEventEnvelopeV1","deliveryOffset","aggregateSequence"]),
 "canon-state":("docs/01-contracts/task-run-state-machines.md",["leaseEpoch","UNKNOWN","recordVersion"]),
 "rest":("docs/08-interfaces/rest-api.md",["If-Match","retry","Idempotency"]),
 "auth":("docs/09-security/authentication-and-user-isolation.md",["owner_private","tenant","cursor"]),
 "db":("docs/10-infrastructure/database-schema-and-migrations.md",["external_subscription_generations","notification_intent_ownership","schema_migrations","record_version"]),
 "watch":("docs/07-automation/watch-lifecycle.md",["definitionRevision","recordVersion","retire"]),
 "learned":("docs/14-adaptive-experience/learned-browser-actions.md",["read_only_click","POST","automatic replay"]),
 "feedback":("docs/14-adaptive-experience/feedback-learning.md",["retailer_storefront","Memory Writer","Undo"]),
}
layerResults=[{"id":k,"path":p,"missing":[z for z in terms if z not in docs.get(p,{}).get("body","")]} for k,(p,terms) in layers.items()]
out={"checked_prs":len(prs),"checked_files":len(docs),"numeric_json_version_violations":numeric,"possible_active_stale_claims":stale,"layer_fingerprint":layerResults,"current_heads":{str(p["number"]):p["head"]["sha"] for p in prs},"disclaimer":"Only text pattern and JSON examples; does not prove semantic consistency."}
folder=pathlib.Path("audit/round6/output");folder.mkdir(parents=True,exist_ok=True)
(folder/"semantic_findings.json").write_text(json.dumps(out,indent=2,ensure_ascii=False))
print("ROUND6_SEMANTIC_STATUS",json.dumps({"prs":len(prs),"docs":len(docs),
 "numeric_json":len(numeric),"possible_stale":len(stale),
 "layer_missing":sum(bool(x["missing"]) for x in layerResults)},ensure_ascii=False))
print("ROUND6_NUMERIC",json.dumps(numeric[:60],ensure_ascii=False))
print("ROUND6_STALE",json.dumps(stale[:45],ensure_ascii=False))
print("ROUND6_LAYER",json.dumps([x for x in layerResults if x["missing"]],ensure_ascii=False))
# Emit evidence; only unambiguous numeric JSON violations fail the gate.
if numeric or any(x["missing"] for x in layerResults):sys.exit(1)
