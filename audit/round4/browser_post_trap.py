#!/usr/bin/env python3
"""Proves a visually read-only click/fill can send POST in a *real local Chromium*.
This does NOT validate the unimplemented Harness network/Tool Gateway firewall.
"""
import http.server, threading, shutil, subprocess, tempfile, json
seen=[]
page=b"""<!doctype html><html><body>
<button id="view">View more</button><input id="search" aria-label="Read only search">
<div id="done">pending</div>
<script>
const view=document.getElementById('view'), field=document.getElementById('search');
const requests=[];
view.addEventListener('click',()=>requests.push(fetch('/write', {method:'POST', body:'view-only-click'})));
field.addEventListener('input',()=>requests.push(fetch('/autosave', {method:'POST', body:'read-only-fill'})));
setTimeout(async ()=>{view.click();field.value='test';field.dispatchEvent(new Event('input',{bubbles:true}));
await Promise.all(requests);document.getElementById('done').textContent='synthetic-POST-occurred';},100);
</script></body></html>"""
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(page)
 def do_POST(self):
  n=int(self.headers.get("Content-Length","0"));payload=self.rfile.read(n)
  seen.append((self.path,payload.decode(errors="replace")))
  self.send_response(204);self.end_headers()
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 binary=next((v for x in ('google-chrome','chromium','chromium-browser') if (v:=shutil.which(x))),None)
 assert binary,"Chromium binary not found"
 with tempfile.TemporaryDirectory() as profile:
  p=subprocess.run([binary,'--headless','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--user-data-dir='+profile,
    '--virtual-time-budget=3500','--dump-dom',f'http://127.0.0.1:{server.server_address[1]}/'],
    capture_output=True,text=True,timeout=40)
 print("BROWSER_POST_TRAP_RC",p.returncode)
 print("SYNTHETIC_LOCAL_POSTS",json.dumps(seen))
 assert p.returncode==0,(p.stderr[-800:])
 assert {r[0] for r in seen}=={'/write','/autosave'},'Local click/fill did not create both POSTs'
 assert 'synthetic-POST-occurred' in p.stdout,'Page did not observe both requests'
 print("PASS threat reproduction: read-only-labelled UI interactions sent two POSTs")
 print("NOT A HARNESS EGRESS TEST; automatic cached click/fill must remain disabled")
finally:
 server.shutdown()
