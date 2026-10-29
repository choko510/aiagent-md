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
prs=[x for x in prs if re.fullmatch(r"spec/chat-(?:0[1-9]|1[0-5])-.+",x["head"]["ref"])]
assert len(prs)==58,f"expected 58 Draft PRs got {len(prs)}"
records=[];done=[]
for p in sorted(prs,key=lambda z:z["number"]):
 n=p["number"]; owner=re.search(r"chat-(\d+)",p["head"]["ref"]).group(1)
 changed=get(f"{API}/pulls/{n}/files?per_page=100")
 docs=[x for x in changed if x["filename"].startswith("docs/") and x["filename"].endswith(".md")]
 for x in docs:
  path=x["filename"]
  content=get(f"{API}/contents/{path}?ref={p['head']['sha']}")
  raw=base64.b64decode(content["content"]).decode("utf-8")
  lines=raw.splitlines()
  unsafe=[{"line":i+1,"code":line.strip()[:130]} for i,line in enumerate(lines)
   if re.search(r"\b(?:goalRevision|leaseEpoch|recordVersion|expectedRecordVersion|definitionRevision|expectedLeaseEpoch|expectedGoalRevision|parentGoalRevision|inheritedGoalRevision|childGoalRevision|resultRevision|restoredGoalRevision|intentRecordVersion)\s*:\s*number\b",line)]
  stale=[{"line":i+1,"code":line.strip()[:130]} for i,line in enumerate(lines)
   if ("task_events" in line or "safe integer" in line.lower() or "P0 unresolved" in line.lower())][:10]
  rec={"pr":n,"owner":owner,"path":path,"sha":p["head"]["sha"],"bytes":len(raw.encode()),"lines":len(lines),
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
counts=collections.Counter(z["owner"] for z in records)
expected={"01":6,"02":12,"03":6,"04":9,"05":5,"06":5,"07":5,"08":6,"09":6,"10":5,"11":4,"12":5,"13":3,"14":2,"15":1}
assert dict(counts)==expected,(counts,expected)
assert len(records)==80,len(records)
assert len({r["path"] for r in records})==80,"overlap file paths"
assert all(x["draft"] for x in done),"all 49 must remain Draft at audit time"
out=ROOT/"audit/round4/output";out.mkdir(parents=True,exist_ok=True)
jsonfile=out/"full_spec_matrix.json"
jsonfile.write_text(json.dumps({"generated_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
 "pr_count":len(prs),"spec_count":len(records),"owner_counts":dict(counts),"pr_status":done,"specs":records},ensure_ascii=False,indent=2))
md=["# Round 4 / all-PR / all-MD latest-HEAD scanner","",
 "Scan is structural/static, not a substitute for independent semantics/OSS verification.",
 f"**PRs: {len(prs)}, specs: {len(records)}, unique paths: {len(set(r['path'] for r in records))}; all 85 planned specs minus CHAT_00's 5 Foundation docs.**","",
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
md+=["","## Review limitations","- Reader fetched all 80 raw Markdown texts from each live PR HEAD on GitHub API.","- Warnings are pattern matches; historical references can be benign.","- Source URLs were counted, not all independently verified against upstream code.","- PostgreSQL/Docker/Browser checks are in other jobs and are NOT actual Agent Harness runtime checks.","- An actual implementation repository/agent deployment does not yet exist in this specs repository."]
(out/"full_spec_matrix.md").write_text("\n".join(md)+"\n")
print("AUDIT_MATRIX_STATUS",json.dumps({"prs":len(prs),"specs":len(records),"owners":dict(counts),
 "unsafeDocs":len(bad),"sourcePinned":sum(r["pinned_source_urls"] for r in records),
 "missingFailure":sum(not r["mentions_failure"] for r in records),
 "missingTests":sum(not r["mentions_tests"] for r in records),
 "minLines":min(r["lines"] for r in records),"maxLines":max(r["lines"] for r in records),"historicalMentions":sum(bool(r['historical_conflict_text']) for r in records),"provisionalP0Mentions":sum(r['integration_proposals'] for r in records)},ensure_ascii=False))
