"""A stand-in for a third-party Anthropic-compatible endpoint (Z.ai/GLM, …).

Logs every request it receives to a JSONL file so tests can assert on what
actually arrived — in particular whether background compaction reached this
endpoint or leaked off to api.anthropic.com.

  python mock_endpoint.py <port> <logfile>

Env:
  MOCK_STRICT=1          reject requests carrying cache_control / tool_choice,
                         the way a shallower /v1/messages implementation does
  MOCK_INPUT_TOKENS=N    input_tokens to report (drives the compression trigger)
  MOCK_COMPACTION_DELAY  seconds to hold a compaction request open, so tests can
                         put a second conversation in flight while the first is
                         still compressing
  MOCK_TOKENS_FROM_SIZE  report input_tokens derived from the actual request
                         instead of a constant, so a conversation that got its
                         summary injected falls back under the trigger the way a
                         real one does. A constant makes every conversation
                         demand compaction forever, which is not a real workload.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]
BIG_INPUT = int(os.environ.get("MOCK_INPUT_TOKENS", "150000"))
STRICT = os.environ.get("MOCK_STRICT") == "1"
COMPACTION_DELAY = float(os.environ.get("MOCK_COMPACTION_DELAY", "0") or 0)
TOKENS_FROM_SIZE = os.environ.get("MOCK_TOKENS_FROM_SIZE") == "1"

COMPACTION_MARKER = "context compressor"
SUMMARY_MARKER = "summary of our earlier conversation"


def note(kind, detail):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "detail": detail}) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        msgs = payload.get("messages") or []
        is_compaction = COMPACTION_MARKER in (json.dumps(msgs[-1]) if msgs else "")
        reported_input = (max(1, len(json.dumps(msgs)) // 4) if TOKENS_FROM_SIZE
                          else BIG_INPUT)

        note("request", {
            "path": self.path,
            "model": payload.get("model"),
            "messages": len(msgs),
            "convo_chars": len(json.dumps(msgs)),
            "compaction": is_compaction,
            "carries_summary": any(SUMMARY_MARKER in json.dumps(m) for m in msgs),
        })

        if STRICT:
            text = raw.decode("utf-8", "replace")
            bad = next((f for f in ("cache_control", '"tool_choice"') if f in text), None)
            if bad:
                note("rejected", {"field": bad, "compaction": is_compaction})
                self._send(400, "application/json", json.dumps({
                    "error": {"type": "invalid_request_error",
                              "message": f"unsupported field: {bad}"}}).encode())
                return

        if is_compaction and COMPACTION_DELAY:
            time.sleep(COMPACTION_DELAY)

        reply = ("SUMMARY: the conversation so far, densely compacted."
                 if is_compaction else "ok")
        model = payload.get("model", "glm-4.6")

        if not payload.get("stream"):
            self._send(200, "application/json", json.dumps({
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": model, "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": reported_input, "output_tokens": 12},
            }).encode())
            return

        events = [
            {"type": "message_start", "message": {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "usage": {"input_tokens": reported_input, "output_tokens": 1,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": reply}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 12}},
            {"type": "message_stop"},
        ]
        body = "".join("event: {}\ndata: {}\n\n".format(e["type"], json.dumps(e))
                       for e in events).encode()
        self._send(200, "text/event-stream", body)


if __name__ == "__main__":
    note("start", {"port": PORT, "strict": STRICT})
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
