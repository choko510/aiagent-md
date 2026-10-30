#!/usr/bin/env python3
"""Round7 combined specs + exact 61 source PR blob verification."""
import pathlib,re,json,sys,subprocess,collections,os,urllib.request,time
ROOT=pathlib.Path(__file__).resolve().parents[2]
text=(ROOT/"docs/00-foundation/spec-index.md").read_text()
required=re.findall(r"\|\s*\d{2}-\d{2}\s*\|\s*"+chr(96)+r"(docs/[0-9]{2}-[^"+chr(96)+r"]+\.md)"+chr(96),text)
found=sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/"docs").glob("[0-9][0-9]-*/*.md"))
err=[];broken=[];numeric=[]
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
"relativeLinkWarnings":len(broken),"int64JsonErrors":len(numeric),"errors":err},ensure_ascii=False))
sys.exit(1 if err else 0)
