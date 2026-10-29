#!/usr/bin/env python3
"""Adversarial XLSX/ZIP *reference parser* within real restricted Docker isolation.
This is not the unimplemented Harness parser sidecar or a full PDF/Office renderer.
"""
import pathlib, tempfile, zipfile, subprocess, textwrap, json
worker=r'''
import pathlib,zipfile,socket,re,sys,json
root=pathlib.Path('/inputs')
def parse(name):
 path=root/name
 with zipfile.ZipFile(path) as z:
  items=z.infolist()
  if len(items)>120:return 'reject_entry_cap'
  total=0
  for i in items:
   # Do not extract to filesystem: read only bounded bytes from archive.
   n=i.filename.replace('\\','/')
   if n.startswith('/') or '..' in n.split('/') or re.match(r'^[A-Za-z]:',n):return 'reject_path_traversal'
   if i.flag_bits & 0x1:return 'reject_encrypted'
   if i.file_size>2*1024*1024 or i.file_size/max(1,i.compress_size)>25:return 'reject_decompress_cap'
   total+=i.file_size
   if total>4*1024*1024:return 'reject_total_cap'
   lower=n.lower()
   if ('vbaproject.bin' in lower or 'externallinks/' in lower or lower.endswith('.js')
       or 'embeddings/' in lower):return 'reject_embedded_active_content'
  suspicious=[]
  for i in items:
   if i.filename.startswith('xl/worksheets/') and i.filename.endswith('.xml'):
    data=z.read(i)
    if b'<f>' in data or b'<f ' in data:
     suspicious.append('formula_literal_no_evaluation')
  return 'review_untrusted_formula' if suspicious else 'accepted_nonexecuting_zip_metadata'
expected={
 'normal.xlsx':'accepted_nonexecuting_zip_metadata',
 'macro.xlsx':'reject_embedded_active_content',
 'bomb.xlsx':'reject_decompress_cap',
 'traversal.xlsx':'reject_path_traversal',
 'external.xlsx':'reject_embedded_active_content',
 'formula.xlsx':'review_untrusted_formula'
}
obs={name:parse(name) for name in expected}
print('PARSER_REFERENCE_OUTCOMES',json.dumps(obs,sort_keys=True),flush=True)
assert obs==expected,(obs,expected)
# A denied network namespace, not mere URL prevalidation.
try:
 socket.create_connection(('1.1.1.1',443),timeout=0.3)
except OSError:
 print('PARSER_NO_NETWORK_PASS',flush=True)
else:
 raise AssertionError('restricted worker unexpectedly reached public IP')
assert not pathlib.Path('/var/run/docker.sock').exists()
print('PASS isolated malicious parser reference fixtures',flush=True)
'''
def make_zip(root,name,items):
 with zipfile.ZipFile(root/name,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as f:
  for path,data in items:f.writestr(path,data)
with tempfile.TemporaryDirectory() as temp:
 root=pathlib.Path(temp)
 (root/'worker.py').write_text(worker,encoding='utf-8')
 make_zip(root,'normal.xlsx',[('xl/worksheets/sheet1.xml',b'<worksheet><v>123</v></worksheet>')])
 make_zip(root,'macro.xlsx',[('xl/vbaProject.bin',b'fake macro blob')])
 make_zip(root,'bomb.xlsx',[('xl/worksheets/sheet1.xml',b'X'*(8*1024*1024))])
 make_zip(root,'traversal.xlsx',[('../etc/cron.d/attacker',b'* * * * * root attacker')])
 make_zip(root,'external.xlsx',[('xl/externalLinks/externalLink1.xml',b'http://127.0.0.1/')])
 make_zip(root,'formula.xlsx',[('xl/worksheets/sheet1.xml',b'<f>WEBSERVICE("https://example.invalid")</f>')])
 # tempfile uses 0700; explicit read/search perms are required for numeric non-root uid 65534.
 root.chmod(0o755)
 for file in root.iterdir():file.chmod(0o644)
 cmd=['docker','run','--rm','--network','none','--read-only','--cap-drop=ALL',
  '--security-opt','no-new-privileges','--user','65534:65534','--pids-limit','64',
  '--memory','128m','--cpus','0.5','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16m',
  '--mount',f'type=bind,src={root},dst=/inputs,readonly',
  'python:3.12-alpine','python','/inputs/worker.py']
 proc=subprocess.run(cmd,text=True,capture_output=True,timeout=90)
 print('PARSER_ISOLATION_RC',proc.returncode)
 print(proc.stdout[-3500:],proc.stderr[-1600:])
 assert proc.returncode==0,'reference parser fixture failed'
 assert 'PASS isolated malicious parser reference fixtures' in proc.stdout
print('NOTE: no real imported Harness parser, PDF OCR, Office format engine or user files were exercised')
