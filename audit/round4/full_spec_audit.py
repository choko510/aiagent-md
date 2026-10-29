#!/usr/bin/env python3
"""Independently read all actual Draft PR HEAD Markdown, audit mechanically, produce matrix.
This is NOT a formal proof of semantic consistency or OSS source-code verification.
"""
import base64, collections, datetime, json, os, pathlib, re, time, urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[2]
R="choko510/aiagent-md"
API=f"https://api.github.com/repos/{R}"
TOKEN=os.getenv("GH_TOKEN","")
def get(url):
 h={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"independent-spec-audit/1.0"}
 if TOKEN:h["Authorization"]="Bearer "+TOKEN
 for attempt in range(3):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=35) as res:return json.loads(res.read())
  except Exception:
   if attempt==2:raise
   time.sleep(2**attempt)
prs=get(API+"/pulls?state=all&per_page=100")
prs=[x for x in prs if re.fullmatch(r"spec/chat-(?:0[0-9]|1[0-5])-.+",x["head"]["ref"])]
assert len(prs)==61,f"expected 61 Draft PRs got {len(prs)}"
records=[];done=[];raw_by_path={}
for p in sorted(prs,key=lambda z:z["number"]):
 n=p["number"]; owner=re.search(r"chat-(\d+)",p["head"]["ref"]).group(1)
 changed=get(f"{API}/pulls/{n}/files?per_page=100")
 docs=[x for x in changed if x["filename"].startswith("docs/") and x["filename"].endswith(".md")]
 for x in docs:
  path=x["filename"]
  content=get(f"{API}/contents/{path}?ref={p['head']['sha']}")
  raw=base64.b64decode(content["content"]).decode("utf-8")
  lines=raw.splitlines()
  raw_by_path[path]=raw
  content_hash=__import__("hashlib").sha256(raw.encode()).hexdigest()
  unsafe=[{"line":i+1,"code":line.strip()[:130]} for i,line in enumerate(lines)
   if re.search(r"\b(?:goalRevision|leaseEpoch|recordVersion|expectedRecordVersion|definitionRevision|expectedLeaseEpoch|expectedGoalRevision|parentGoalRevision|inheritedGoalRevision|childGoalRevision|resultRevision|restoredGoalRevision|intentRecordVersion)\s*:\s*number\b",line)]
  stale=[{"line":i+1,"code":line.strip()[:130]} for i,line in enumerate(lines)
   if ("task_events" in line or "safe integer" in line.lower() or "P0 unresolved" in line.lower())][:10]
  rec={"pr":n,"owner":owner,"path":path,"sha":p["head"]["sha"],"bytes":len(raw.encode()),"lines":len(lines),
   "sha256":content_hash,
   "section_count":sum(bool(re.match(r"^## ",v)) for v in lines),
   "code_blocks":sum(bool(re.match(r"^(?:```|~~~)",v)) for v in lines)//2,
   "source_urls":len(re.findall(r"https?://[^\s)]+",raw)),
   "pinned_source_urls":len(re.findall(r"github\.com/[^\s)]+/blob/[0-9a-f]{40}",raw,re.I)),
   "mentions_tests":bool(re.search(r"(?i)\bTEST|テスト|試験",raw)),
   "mentions_failure":bool(re.search(r"(?i)障害|failure|fault|timeout",raw)),
   "unsafe_version_number":unsafe,"possible_stale_text":stale,
   "historical_conflict_text":[{"line":i+1,"text":line.strip()[:230]} for i,line in enumerate(lines) if re.search(r"(?:UUIDv7.{0,85}gen_random_uuid|conversationId.{0,85}NOT NULL|recordVersion.{0,65}revision.{0,35}0)",line,re.I)][:12],
   "integration_proposals":len(re.findall(r"\bP0[-_]|\bP0\s+[A-Z]|\bP0\b",raw)),
   "source_file_claims":len(re.findall(r"(?:github\.com/[\w.-]+/[\w.-]+/blob/[0-9a-f]{40}/|[\w./-]+\.ts|[\w./-]+\.py)",raw))}
  records.append(rec)
 done.append({"pr":n,"owner":owner,"files":len(docs),"draft":p["draft"],"head":p["head"]["sha"]})

# Round5 semantic guardrail scan. No claim of full formal proof; these are
# precise inter-document obligations checked against every LIVE PR HEAD.
owner_dir={f"{n:02d}":v for n,v in enumerate([
 "00-foundation","01-contracts","02-runtime","03-tools","04-web-browser","05-connectors",
 "06-memory-skills","07-automation","08-interfaces","09-security","10-infrastructure",
 "11-qa","12-intelligence","13-event-ingestion","14-adaptive-experience","15-multimodal"])}
path_violations=[r for r in records if not r["path"].startswith("docs/"+owner_dir[r["owner"]]+"/")]
# This set is intentionally tied to the highest-risk cross-module contracts,
# not to superficial presence of markdown headings or polished prose.
obligations={
 "docs/10-infrastructure/database-schema-and-migrations.md":
   ["external_subscription_generations","external_event_receipts",
    "notification_intent_ownership","digest_active_member_only",
    "parser_jobs","change_baselines","memory_feedback_mutations",
    "schema_migrations","source_attempt_receipts"],
 "docs/13-event-ingestion/event-driven-agent.md":["subscriptionGeneration","nativeEventId","receipt","revoke"],
 "docs/13-event-ingestion/notification-intelligence.md":
   ["notification_intent_ownership","coverage_unknown","CHAT_07"],
 "docs/07-automation/notification-outbox.md":
   ["notification_intent_ownership","coverage_unknown","UNKNOWN"],
 "docs/12-intelligence/entity-resolution.md":
   ["retailer_storefront","mappingRevision","REVIEW"],
 "docs/14-adaptive-experience/feedback-learning.md":
   ["retailer_storefront","Memory Writer","Undo"],
 "docs/14-adaptive-experience/learned-browser-actions.md":
   ["read_only_click","read_only_fill","automatic replay","POST"],
 "docs/01-contracts/evidence-artifact-contracts.md":
   ["TransformLocatorV1","sourceSha256","CitationLocator"],
 "docs/15-multimodal/multimodal-ingestion.md":
   ["parser_jobs","sourceSha256","no-network"],
 "docs/13-event-ingestion/change-intelligence.md":
   ["change_baselines","scope_digest","unknown"],
 "docs/11-qa/end-to-end-acceptance.md":
   ["QA-R5-T01","QA-R5-T02","DomainEvent"],
}
contract_findings=[]
for path,parts in obligations.items():
 raw=raw_by_path.get(path,"")
 missing=[term for term in parts if term not in raw]
 contract_findings.append({"path":path,"missing":missing,"pass":bool(raw) and not missing})
active_drift=[]
for r in records:
 lines=raw_by_path[r["path"]].splitlines()
 for i,line in enumerate(lines):
  if (re.search(r"\\b(?:entityMappingRevision|mappingRevision|goalRevision|leaseEpoch)\\s*:\\s*number\\b",line) or
      (re.search(r"\\b(?:P0|未整合|共有P0)\\b",line) and re.search(r"gen_random_uuid\\(\\)|nullable.*NOT NULL|recordVersion.*revision.*0",line,re.I)
       and not re.search(r"旧|historical|過去|Round1|resolved|解消|修正済み",line,re.I))):
   active_drift.append({"path":r["path"],"pr":r["pr"],"line":i+1,"text":line[:240]})
# Warnings are independent human-review leads, not automatically currently active bugs.
assert not path_violations,"Incorrect owner paths: "+str(path_violations[:4])
assert all(x["pass"] for x in contract_findings),"Missing Round5 contract references: "+str([x for x in contract_findings if not x["pass"]])

counts=collections.Counter(z["owner"] for z in records)
expected={"00":5,"01":6,"02":12,"03":6,"04":9,"05":5,"06":5,"07":5,"08":6,"09":6,"10":5,"11":4,"12":5,"13":3,"14":2,"15":1}
assert dict(counts)==expected,(counts,expected)
assert len(records)==85,len(records)
assert len({r["path"] for r in records})==85,"overlap file paths"
assert all(x["draft"] for x in done),"all 61 must remain Draft at audit time"
out=ROOT/"audit/round4/output";out.mkdir(parents=True,exist_ok=True)
jsonfile=out/"full_spec_matrix.json"
jsonfile.write_text(json.dumps({"generated_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
 "pr_count":len(prs),"spec_count":len(records),"owner_counts":dict(counts),"pr_status":done,"specs":records},ensure_ascii=False,indent=2))
md=["# Round 4 / all-PR / all-MD latest-HEAD scanner","",
 "Scan is structural/static, not a substitute for independent semantics/OSS verification.",
 f"**PRs: {len(prs)}, specs: {len(records)}, unique paths: {len(set(r['path'] for r in records))}; all 85 planned specs including CHAT_00 Foundation docs.**","",
 "| PR | Owner | MD | Lines | Pinned source refs | Unsafe int64 fields |",
 "|---:|---|---|---:|---:|---:|"]
for r in records:md.append(f"| #{r['pr']} | CHAT_{r['owner']} | `{r['path']}` | {r['lines']} | {r['pinned_source_urls']} | {len(r['unsafe_version_number'])} |")
bad=[r for r in records if r["unsafe_version_number"]]
md+=["","## Remaining unsafe Version/Epoch number fields",f"Detected in {len(bad)} MD(s)."]
for r in bad:
 md.append(f"### PR #{r['pr']} — {r['path']}")
 md.extend([f"- L{x['line']}: `{x['code']}`" for x in r["unsafe_version_number"]])
md+=["","## Historical conflict mentions requiring owner-level recheck","Old UUIDv7/NOT NULL/revision0 assertions must not be treated as current if corrected in latest PR #10."]
for r in records:
 if r["historical_conflict_text"]:
  md.append(f"### PR #{r['pr']} — {r['path']}")
  md.extend([f"- L{x['line']}: `{x['text']}`" for x in r["historical_conflict_text"]])
md+=["","## Review limitations","- Reader fetched all 85 raw Markdown texts from each live PR HEAD on GitHub API.","- Warnings are pattern matches; historical references can be benign.","- Source URLs were counted, not all independently verified against upstream code.","- PostgreSQL/Docker/Browser checks are in other jobs and are NOT actual Agent Harness runtime checks.","- An actual implementation repository/agent deployment does not yet exist in this specs repository."]
md+=["","## Round 5: Cross-owner semantic presence gates (not formal correctness proofs)","",
 "| Path | Contract markers | Result |","|---|---:|---|"]
for x in contract_findings:md.append(f"| \`{x['path']}\` | {len(obligations[x['path']])} | {'PASS' if x['pass'] else 'FAIL'} |")
md+=["","## Round 5: Potential outdated **active** assertions",""
 ,"Listed lines need individual owner review: documentation may be historical or provisional."]
for z in active_drift[:100]:md.append(f"- PR #{z['pr']} \`{z['path']}\` L{z['line']}: \`{z['text'].replace(chr(96),chr(39))}\`")
(out/"round5_contract_gates.json").write_text(json.dumps({
 "obligations":contract_findings,"owner_path_violations":path_violations,
 "outdated_current_assertions":active_drift,
 "runtime_validation":"NOT IMPLEMENTED / NOT VERIFIED",
 "source_note":"String-matching of actual latest PR HEADs is NOT an independent semantic or runtime proof."
},ensure_ascii=False,indent=2))
(out/"full_spec_matrix.md").write_text("\n".join(md)+"\n")
print("AUDIT_MATRIX_STATUS",json.dumps({"prs":len(prs),"specs":len(records),"owners":dict(counts),
 "unsafeDocs":len(bad),"sourcePinned":sum(r["pinned_source_urls"] for r in records),
 "missingFailure":sum(not r["mentions_failure"] for r in records),
 "missingTests":sum(not r["mentions_tests"] for r in records),
 "minLines":min(r["lines"] for r in records),"maxLines":max(r["lines"] for r in records),"foundationDocs":sum(r['owner']=='00' for r in records),"historicalMentions":sum(bool(r['historical_conflict_text']) for r in records),"provisionalP0Mentions":sum(r['integration_proposals'] for r in records),"ownerPathViolations":len(path_violations),
 "round5ContractGatesPassed":sum(x["pass"] for x in contract_findings),
 "round5ContractGatesTotal":len(contract_findings),
 "potentialActiveHistoricalAssertions":len(active_drift)},ensure_ascii=False))
