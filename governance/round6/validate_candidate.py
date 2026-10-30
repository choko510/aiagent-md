#!/usr/bin/env python3
"""Check archived source-PR SHA map and 85 spec blobs on merged main.
This verifies documentation provenance, not cross-owner Locked approval or runtime.
"""
import json,pathlib,collections,os,urllib.request,time,sys,re
repo="choko510/aiagent-md";api=f"https://api.github.com/repos/{repo}"
root=pathlib.Path(__file__).resolve().parent
manifest=json.loads((root/"contract-lock-candidate.json").read_text())
token=os.getenv("GH_TOKEN","")
def get(path):
 headers={"Accept":"application/vnd.github+json","User-Agent":"round6-merged-source-audit"}
 if token:headers["Authorization"]="Bearer "+token
 for attempt in range(4):
  try:
   req=urllib.request.Request(api+path,headers=headers)
   with urllib.request.urlopen(req,timeout=50) as f:return json.loads(f.read())
  except Exception:
   if attempt==3:raise
   time.sleep(2**attempt)
prs=get("/pulls?state=all&per_page=100")
by={p["number"]:p for p in prs}
tree=get("/git/trees/main?recursive=1")
main_blobs={x["path"]:x["sha"] for x in tree["tree"] if x["type"]=="blob"}
rows=manifest["prs"]
errors=[]
if len(rows)!=61 or len({p["number"] for p in rows})!=61:errors.append("61 unique PR rows missing")
if manifest["status"]!="INTEGRATED_DOCUMENTS_CONTRACTS_UNRATIFIED":errors.append("incorrect governance state")
if manifest["ownerApprovalStatus"]!="NOT_GRANTED" or manifest["runtimeStatus"]!="NOT_IMPLEMENTED":errors.append("false contract/runtime approval")
if manifest["mergePolicy"]!="NO_AUTOMERGE":errors.append("unsafe automatic merge claim")
owners={};source_paths={}
for p in rows:
 n=p["number"]
 actual=by.get(n)
 if not actual:errors.append(f"PR #{n} missing");continue
 if not actual.get("merged_at"):errors.append(f"PR #{n} not actually merged")
 if actual["head"]["sha"]!=p["headSha"]:errors.append(f"PR #{n} HEAD changed")
 if actual["head"]["ref"]!=p["branch"]:errors.append(f"PR #{n} branch drift")
 if not p.get("merged") or p.get("locked") or p.get("approvedBy") or p.get("approvalEvidence"):
  errors.append(f"PR #{n} manifest misstates merge/approval")
 for entry in get(f"/pulls/{n}/files?per_page=100"):
  path=entry["filename"]
  if not path.startswith("docs/") or not path.endswith(".md"):continue
  if path in source_paths:errors.append(f"duplicate module source path {path}")
  source_paths[path]=n
  owners[path]=p["owner"]
  if main_blobs.get(path)!=entry["sha"]:errors.append(f"main blob SHA mismatch {path}")
expected={"00":5,"01":6,"02":12,"03":6,"04":9,"05":5,"06":5,"07":5,"08":6,"09":6,"10":5,"11":4,"12":5,"13":3,"14":2,"15":1}
counts=dict(collections.Counter(owners.values()))
if len(source_paths)!=85 or counts!=expected:
 errors.append(f"85 spec owner counts: {len(source_paths)}, {counts}")
print("MERGED_SPEC_PROVENANCE",json.dumps({"mergedSourcePRs":len(rows),"moduleBlobs":len(source_paths),"mainSha":tree["sha"],"ownerCounts":counts,
 "governance":"UNRATIFIED","runtime":"NOT_IMPLEMENTED","errors":errors[:25]}))
if errors:sys.exit(1)
print("PASS merged original PRs and all 85 identical module content blobs on main.")
