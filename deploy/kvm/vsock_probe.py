"""Probe the execution VM supervisor over vsock: it must answer, and must reject an unauthenticated request."""

import json
import socket
import struct
import sys

cid, port = int(sys.argv[1]), int(sys.argv[2])
s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
s.settimeout(5)
s.connect((cid, port))
s.sendall(struct.pack(">I", 2) + b"{}")
n = struct.unpack(">I", s.recv(4))[0]
reply = json.loads(s.recv(n))
sys.exit(0 if reply.get("code") == "UNAUTHENTICATED" else 1)
