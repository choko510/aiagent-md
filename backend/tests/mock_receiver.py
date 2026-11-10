"""The fake external world: an HTTP message receiver. Every POST that reaches it is ONE irreversible external effect
and is appended to a JSONL log (raw count, no dedupe), so tests can prove "external sends == 1".

  python mock_receiver.py <port> <logfile> [hang]
POST /messages        -> {"receipt": ...}   (hang mode: record the effect, then never answer = ACK lost)
GET  /receipts/<key>  -> 200 / 404           (reconciliation)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys


async def main(port: int, log: str, hang: bool, token: str) -> None:
    seen: dict[str, str] = {}

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = (await r.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            method, path, _ = head[0].split(" ", 2)
            h = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in head[1:] if ln)}
            body = await r.readexactly(int(h.get("content-length", "0")))
            key = h.get("idempotency-key", "")
            if method == "POST" and path == "/messages":
                rcpt = "rcpt_" + hashlib.sha256(key.encode()).hexdigest()[:12]
                seen[key] = rcpt
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "body": json.loads(body), "authorized":
                                        h.get("authorization") == f"Bearer {token}" if token else None,
                                        "via_proxy_auth": "proxy-authorization" in h}) + "\n")
                if hang:
                    await asyncio.sleep(3600)
                out, code = json.dumps({"receipt": rcpt}).encode(), 200
            elif method == "GET" and path.startswith("/receipts/"):
                k = path[len("/receipts/"):]
                out, code = (json.dumps({"receipt": seen[k]}).encode(), 200) if k in seen else (b"{}", 404)
            else:
                out, code = b"{}", 404
            w.write(f"HTTP/1.1 {code} X\r\ncontent-type: application/json\r\ncontent-length: {len(out)}\r\n"
                    "connection: close\r\n\r\n".encode() + out)
            await w.drain()
        except Exception:
            pass
        finally:
            w.close()

    srv = await asyncio.start_server(handle, os.environ.get("MOCK_RECEIVER_HOST", "127.0.0.1"), port)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], len(sys.argv) > 3 and sys.argv[3] == "hang",
                     sys.argv[4] if len(sys.argv) > 4 else ""))
