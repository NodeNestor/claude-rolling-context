"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Pure stdlib — no external dependencies needed.
"""

import hashlib
import json
import os
import sys
import logging
import threading
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

from compressor import RollingCompressor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rolling-context")

UPSTREAM_URL = os.environ.get("ROLLING_CONTEXT_UPSTREAM", "https://api.anthropic.com")
LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT", "5588"))
TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER", "80000"))
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET", "40000"))
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL", "claude-haiku-4-5-20251001")

# SSL context for upstream HTTPS requests
ssl_ctx = ssl.create_default_context()

compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
)


class SessionTracker:
    """Tracks compression state per session."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def _fingerprint(self, messages: list) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content)
                return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        return "unknown"

    def get(self, messages: list) -> dict:
        fp = self._fingerprint(messages)
        with self._lock:
            if fp not in self._sessions:
                self._sessions[fp] = {
                    "pending": None,
                    "pending_msg_count": 0,
                    "thread": None,
                    "last_input_tokens": 0,
                }
            return self._sessions[fp]

    def cleanup_stale(self, max_sessions: int = 50):
        with self._lock:
            if len(self._sessions) > max_sessions:
                to_remove = []
                for fp, state in self._sessions.items():
                    if state["pending"] is None and (state["thread"] is None or not state["thread"].is_alive()):
                        to_remove.append(fp)
                for fp in to_remove[:len(self._sessions) - max_sessions]:
                    del self._sessions[fp]


tracker = SessionTracker()


def _forward_headers(req_headers: dict, body: bytes = None) -> dict:
    headers = {}
    for key, value in req_headers.items():
        if key.lower() != "host":
            headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    return headers


def get_passthrough_headers(req_headers: dict) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


def _validate_tool_pairs(messages: list) -> list:
    tool_use_ids = set()
    valid_from = 0
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        if block.get("tool_use_id", "") not in tool_use_ids:
                            valid_from = i + 1
    if valid_from > 0:
        log.info(f"Dropping {valid_from} messages with orphaned tool_result references")
    return messages[valid_from:]


def _do_background_compression(session_state: dict, messages: list, msg_count: int, auth_headers: dict):
    try:
        compressed = compressor.compress(messages, auth_headers)
        session_state["pending"] = compressed
        session_state["pending_msg_count"] = msg_count
        log.info(
            f"Background compression ready: "
            f"~{compressor.estimate_tokens(compressed):,} tokens "
            f"({len(compressed)} messages, based on {msg_count} original)"
        )
    except Exception as e:
        log.error(f"Background compression failed: {e}")
        session_state["pending"] = None


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    # Suppress default access log
    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _proxy_request(self, method: str):
        """Generic proxy for non-messages endpoints."""
        body = self._read_body()
        headers = _forward_headers(self._get_headers_dict(), body if body else None)
        target_url = f"{UPSTREAM_URL}{self.path}"

        try:
            req = Request(target_url, data=body if body else None, headers=headers, method=method)
            with urlopen(req, context=ssl_ctx, timeout=300) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in ("transfer-encoding", "content-encoding", "connection"):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(resp_body)
        except URLError as e:
            log.error(f"Upstream error: {e}")
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_GET(self):
        if self.path == "/health":
            self._handle_health()
        else:
            self._proxy_request("GET")

    def do_POST(self):
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_DELETE(self):
        self._proxy_request("DELETE")

    def do_PATCH(self):
        self._proxy_request("PATCH")

    def do_OPTIONS(self):
        self._proxy_request("OPTIONS")

    def _handle_health(self):
        active = sum(
            1 for s in tracker._sessions.values()
            if s["thread"] is not None and s["thread"].is_alive()
        )
        data = {
            "status": "ok",
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "summarizer_model": SUMMARIZER_MODEL,
            "upstream_url": UPSTREAM_URL,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "active_sessions": len(tracker._sessions),
            "active_compressions": active,
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self):
        raw_body = self._read_body()
        req_headers = self._get_headers_dict()
        auth_headers = get_passthrough_headers(req_headers)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"Invalid JSON"}')
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        session_state = tracker.get(messages)

        estimated = compressor.estimate_tokens(messages)
        token_count = session_state["last_input_tokens"] if session_state["last_input_tokens"] > 0 else estimated

        # Apply pending compression
        if session_state["pending"] is not None:
            compressed = session_state["pending"]
            original_count = session_state["pending_msg_count"]
            session_state["pending"] = None
            session_state["pending_msg_count"] = 0

            new_messages = messages[original_count:] if original_count < len(messages) else []
            merged = compressed + new_messages
            merged = _validate_tool_pairs(merged)
            merged_tokens = compressor.estimate_tokens(merged)

            if merged_tokens < token_count:
                for msg in merged:
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                block.pop("cache_control", None)
                log.info(
                    f"Applying compression: ~{token_count:,} -> ~{merged_tokens:,} tokens "
                    f"({len(messages)} -> {len(merged)} messages, "
                    f"{len(new_messages)} new messages appended)"
                )
                payload["messages"] = merged
                token_count = merged_tokens
                session_state["last_input_tokens"] = 0

        # Trigger background compression
        current_messages = payload.get("messages", messages)
        msg_estimate = compressor.estimate_tokens(current_messages)
        already_compressing = session_state["thread"] is not None and session_state["thread"].is_alive()

        if token_count > TRIGGER_TOKENS and msg_estimate > TARGET_TOKENS and not already_compressing:
            log.info(
                f"Context at ~{token_count:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                f"Compressing in background..."
            )
            t = threading.Thread(
                target=_do_background_compression,
                args=(session_state, current_messages, len(current_messages), auth_headers),
                daemon=True,
            )
            t.start()
            session_state["thread"] = t

        # Forward request
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body)
        target_url = f"{UPSTREAM_URL}{self.path}"

        tracker.cleanup_stale()

        try:
            req = Request(target_url, data=body, headers=headers, method="POST")
            with urlopen(req, context=ssl_ctx, timeout=600) as resp:
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() not in ("transfer-encoding", "content-encoding", "connection"):
                        self.send_header(key, value)
                self.end_headers()

                if is_streaming:
                    # Stream chunks and buffer for token extraction
                    buffer = b""
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        buffer += chunk

                    # Extract tokens from SSE
                    try:
                        text = buffer.decode("utf-8", errors="replace")
                        for line in text.split("\n"):
                            if line.startswith("data: ") and '"message_start"' in line:
                                data = json.loads(line[6:])
                                usage = data.get("message", {}).get("usage", {})
                                total_input = (
                                    usage.get("input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0)
                                    + usage.get("cache_read_input_tokens", 0)
                                )
                                if total_input > 0:
                                    session_state["last_input_tokens"] = total_input
                                break
                    except Exception:
                        pass
                else:
                    resp_body = resp.read()
                    self.wfile.write(resp_body)
                    try:
                        data = json.loads(resp_body)
                        usage = data.get("usage", {})
                        total_input = (
                            usage.get("input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                        )
                        if total_input > 0:
                            session_state["last_input_tokens"] = total_input
                    except Exception:
                        pass

        except URLError as e:
            log.error(f"Upstream error: {e}")
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")

    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
