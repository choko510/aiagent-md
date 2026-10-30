#!/usr/bin/env python3
"""Verify a SHA-bound *unapproved* contract lock candidate against live GitHub.
Passing proves inventory freshness only; no authority or runtime proof.
"""
import json, pathlib, os, urllib.request, time, re, sys, collections
repo="choko510/aiagent-md";api=f"https://api.github.com/repos/{repo}"
root=pathlib.Path(__file__).resolve().parent
m=json.loads((root/"contract-lock-candidate.json").read_text())
token=os.environ.get("GH_TOKEN","")
def get(url):
 hdr={"Accept":"application/vnd.github+json","User-Agent":"round6-candidate-gate"}
 if token:hdr["Authorization"]="Bearer "+token
 for attempt in range(4):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers=hdr),timeout=40) as f:return json.loads(f.read())
  except Exception:
   if attempt==3:raise
   time.sleep(2**attempt)
prs=[p for p in get(api+"/pulls?state=open&per_page=100")
 if re.fullmatch(r"spec/chat-(?:0[0-9]|1[0-5])-.+",p["head"]["ref"])]
listed=m["prs"]
assert len(prs)==61 and len(listed)==61,(len(prs),len(listed))
byid={p["number"]:p for p in prs}
assert len(set(p["number"] for p in listed))==61
errors=[]
for p in listed:
 actual=byid.get(p["number"])
 if not actual: errors.append(f"PR #{p['number']} not present/open");continue
 if (p["headSha"]!=actual["head"]["sha"] or p["branch"]!=actual["head"]["ref"]):
  errors.append(f"PR #{p['number']} HEAD changed: manifest={p['headSha']} actual={actual['head']['sha']}")
 if actual["base"]["ref"]!="main" or not actual["draft"]:errors.append(f"PR #{p['number']} no longer draft/main")
 if p["locked"] or p["approvedBy"] or p["approvalEvidence"]:errors.append(f"PR #{p['number']} falsely pre-approved")
 if p["gate"] not in ["CROSS_OWNER_CONTRACT_REVIEW","OWNER_REVIEW"]:errors.append(f"bad gate #{p['number']}")
expected={"00":5,"01":6,"02":12,"03":6,"04":9,"05":5,"06":5,"07":5,"08":6,"09":6,"10":5,"11":4,"12":5,"13":3,"14":2,"15":1}
# 61 PR file listings to prevent confusing a PR inventory with 85 unique spec files.
owned_files={}
for p in listed:
 actual=byid[p["number"]]
 files=get(api+f"/pulls/{p['number']}/files?per_page=100")
 for f in files:
  path=f["filename"]
  if not path.endswith(".md") or not path.startswith("docs/"):continue
  if path in owned_files:errors.append(f"overlapping spec file {path}")
  owned_files[path]=p["owner"]
counts=collections.Counter(owned_files.values())
if dict(counts)!=expected: errors.append(f"count mismatch {dict(counts)}")
if len(owned_files)!=85:errors.append(f"expected 85 docs, got {len(owned_files)}")
if m["status"]!="UNAPPROVED_REVIEW_CANDIDATE" or m["ownerApprovalStatus"]!="NOT_GRANTED" or m["runtimeStatus"]!="NOT_IMPLEMENTED":
 errors.append("Manifest is not an unapproved candidate")
if m["mergePolicy"]!="NO_AUTOMERGE":errors.append("Unsafe merge policy")
print("ROUND6_RATIFICATION_CANDIDATE_CHECK",json.dumps({"prs":len(prs),"docs":len(owned_files),
 "ownerCounts":dict(counts),"headMismatchCount":len([e for e in errors if "HEAD changed" in e]),
 "errors":errors[:30],"lockStatus":"NOT_APPROVED","runtimeStatus":"NOT_IMPLEMENTED"}))
if errors:sys.exit(1)
print("PASS SHA-bound PR inventory; human owner reviews and signatures remain mandatory.")
