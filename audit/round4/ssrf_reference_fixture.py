#!/usr/bin/env python3
"""SSRF *reference policy* negative fixtures. NOT the nonexistent Harness EgressGuard."""
import ipaddress, urllib.parse, http.server, threading, socket, sys
class SSRFRejected(Exception): pass
class MockHandler(http.server.BaseHTTPRequestHandler):
 hits=0
 def do_GET(self):
  MockHandler.hits+=1
  self.send_response(200);self.end_headers();self.wfile.write(b'SECRET')
 def log_message(self,*_):pass

def gate(url,answers,connect_ip=None):
 p=urllib.parse.urlsplit(url)
 if p.scheme not in ("http","https") or not p.hostname or p.username or p.password:
  raise SSRFRejected("scheme/host/userinfo")
 hostname=p.hostname.rstrip(".")
 if "%" in hostname:raise SSRFRejected("zone / escaped host")
 resolved=answers(hostname)
 if not resolved:raise SSRFRejected("unresolved DNS")
 for value in resolved:
  try:ip=ipaddress.ip_address(value)
  except ValueError:raise SSRFRejected("invalid address")
  if not ip.is_global:raise SSRFRejected("non-global address")
 if connect_ip is not None and str(ipaddress.ip_address(connect_ip)) not in resolved:
  raise SSRFRejected("DNS rebinding / pin mismatch")
 return True

def must_block(label,url,answers,connect_ip=None):
 try:gate(url,answers,connect_ip);raise AssertionError("BYPASS "+label)
 except SSRFRejected as exc:print("REFERENCE_SSRF_BLOCKED",label,str(exc))
def public(host):return ["93.184.216.34"]
server=http.server.ThreadingHTTPServer(("127.0.0.1",0),MockHandler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 for name,ip in [("loopback4","127.0.0.1"),("rfc1918","10.1.0.2"),("linklocal","169.254.169.254"),
                 ("loopback6","::1"),("linklocal6","fe80::1"),("unique_local6","fc00::1")]:
  literal="["+ip+"]" if ":" in ip else ip
  must_block(name,"http://"+literal+"/private",lambda host, ip=ip:[ip])
 must_block("userinfo","http://admin:password@public.example/test",public)
 must_block("file_scheme","file:///etc/passwd",public)
 must_block("private_dns","http://internal.example/private",lambda host:["10.0.0.12"])
 must_block("dns_rebinding","http://public.example/data",public,connect_ip="127.0.0.1")
 # Each redirect destination must pass guard before it is requested.
 assert gate("https://public.example/start",public)
 must_block("redirect_to_metadata","http://169.254.169.254/latest/meta-data",lambda host:["169.254.169.254"])
 must_block("local_mock_http","http://127.0.0.1:"+str(server.server_address[1])+"/secret",lambda host:["127.0.0.1"])
 assert MockHandler.hits==0, "reference guard attempted blocked local network access"
 print("REFERENCE_SSRF_PASS 12 negative cases, local protected listener received zero requests")
finally:server.shutdown()
