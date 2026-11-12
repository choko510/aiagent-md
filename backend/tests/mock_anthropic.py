"""Local Provider-compatible test server (Anthropic Messages API subset, streaming SSE).

This is NOT the real provider and makes no external call. It lets the Model
Gateway's ordinary AnthropicProvider code path be exercised end to end (HTTP,
SSE parsing, tool-call parsing, usage, UNKNOWN on a lost ACK) without a
credential or paid traffic.

  python mock_anthropic.py <port> <logfile> [ok|hang|error]

POST /v1/messages  -> SSE message stream; appends one JSONL request record.
GET  /count         -> {"count": n}
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = ""
MODE = "ok"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: object) -> None:  # keep test output clean
        return

    def _record(self, body: dict) -> None:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "model": body.get("model"),
                        "stream": body.get("stream"),
                        "message_count": len(body.get("messages") or []),
                        "has_system": bool(body.get("system")),
                        "has_api_key": bool(self.headers.get("x-api-key")),
                        "proxy_authorization_forwarded": bool(self.headers.get("proxy-authorization")),
                        # content and credential values are deliberately NOT logged.
                        "chars": sum(len(str(m)) for m in body.get("messages") or []),
                    }
                )
                + "\n"
            )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/count":
            try:
                with open(LOG, encoding="utf-8") as f:
                    n = sum(1 for line in f if line.strip())
            except OSError:
                n = 0
            payload = json.dumps({"count": n}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/messages":
            self.send_error(404)
            return
        n = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        self._record(body)
        if MODE == "error":
            self.send_response(529)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        def ev(obj: dict) -> None:
            self.wfile.write(f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        ev(
            {
                "type": "message_start",
                "message": {"id": "msg_mock", "usage": {"input_tokens": 11, "output_tokens": 0}},
            }
        )
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        text = "context-answer: 42"
        for i in range(0, len(text), 6):
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i : i + 6]}})
        ev({"type": "content_block_stop", "index": 0})
        if MODE == "hang":
            # The provider accepted and may have produced the answer, but the client
            # never receives message_stop: the gateway outcome is UNKNOWN.
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
            return
        ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": len(text) // 4}})
        ev({"type": "message_stop"})

    def do_CONNECT(self) -> None:  # noqa: N802
        self.send_error(405)


def main() -> None:
    global LOG, MODE
    port, LOG = int(sys.argv[1]), sys.argv[2]
    MODE = sys.argv[3] if len(sys.argv) > 3 else "ok"
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
