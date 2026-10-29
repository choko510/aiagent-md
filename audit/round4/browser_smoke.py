#!/usr/bin/env python3
"""Browser navigation smoke only. Does NOT test the unfinished Harness EgressGuard."""
import http.server, threading, shutil, subprocess, tempfile, pathlib
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200)
  self.send_header("Content-Type","text/html")
  self.end_headers()
  self.wfile.write(b"<html><body>ROUND4_BROWSER_FIXTURE_OK</body></html>")
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
 bin=next((p for s in ['google-chrome','chromium','chromium-browser'] if (p:=shutil.which(s))),None)
 assert bin,"Chromium not installed on runner"
 with tempfile.TemporaryDirectory() as profile:
  p=subprocess.run([bin,'--headless','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--user-data-dir='+profile,
     '--dump-dom',f'http://127.0.0.1:{server.server_address[1]}/'],
    capture_output=True,text=True,timeout=45)
  print('BROWSER_RC',p.returncode,'BROWSER_DOM_MATCH','ROUND4_BROWSER_FIXTURE_OK' in p.stdout)
  print('BROWSER_STDERR_LAST',p.stderr[-1000:])
  assert p.returncode==0 and 'ROUND4_BROWSER_FIXTURE_OK' in p.stdout
finally:
 server.shutdown()
