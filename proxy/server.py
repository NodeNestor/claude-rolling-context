"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Uses content-based matching: hashes each message, recognizes previously compressed
messages by their content, and replaces them with the compressed version.
No sessions, no fingerprints — just content recognition.

Pure stdlib — no external dependencies needed.
"""

import hashlib
import json
import os
import sys
import logging
import threading
import ssl
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from compressor import RollingCompressor

class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
_log_handler = FlushFileHandler(_log_path, mode="a")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), _log_handler],
)
log = logging.getLogger("rolling-context")

UPSTREAM_URL = os.environ.get("ROLLING_CONTEXT_UPSTREAM", "https://api.anthropic.com")
LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT", "5588"))
TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER", "80000"))
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET", "40000"))
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL", "claude-haiku-4-5-20251001")

ssl_ctx = ssl.create_default_context()
_parsed_upstream = urlparse(UPSTREAM_URL)

compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
)


def _upstream_conn():
    """Create a connection to the upstream server."""
    if _parsed_upstream.scheme == "https":
        return http.client.HTTPSConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 443,
            context=ssl_ctx,
            timeout=600,
        )
    else:
        return http.client.HTTPConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 80,
            timeout=600,
        )


# ---------------------------------------------------------------------------
# Content-based matching
# ---------------------------------------------------------------------------

def _normalize_content(content):
    """Strip volatile metadata (cache_control) for stable hashing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                b = {}
                for k, v in block.items():
                    if k == "cache_control":
                        continue
                    if k == "content" and isinstance(v, (list, str)):
                        b[k] = _normalize_content(v)
                    else:
                        b[k] = v
                result.append(b)
            else:
                result.append(block)
        return result
    return content


def _hash_message(msg: dict) -> str:
    """Stable hash of a message, ignoring cache_control metadata."""
    role = msg.get("role", "")
    content = _normalize_content(msg.get("content", ""))
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    raw = f"{role}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_messages(messages: list) -> list:
    return [_hash_message(m) for m in messages]


class CompressionStore:
    """Content-based compression tracking. No sessions, no fingerprints.

    Recognizes messages by their content hashes. When we compress messages,
    we store hashes of the originals. On future requests, if we see those
    same messages, we replace them with the compressed version.

    Keyed by hash of first message — naturally separates conversations.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}  # first_msg_hash -> entry

    def get_or_create(self, messages: list) -> dict:
        if not messages:
            return self._new_entry()
        key = _hash_message(messages[0])
        with self._lock:
            if key not in self._entries:
                self._entries[key] = self._new_entry()
            return self._entries[key]

    def _new_entry(self):
        return {
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "thread": None,          # background compression thread
            "last_input_tokens": 0,  # API-reported token count
        }

    @property
    def entries(self):
        return self._entries


store = CompressionStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _forward_headers(req_headers: dict, body: bytes = None, strip_encoding: bool = False) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower in ("host", "transfer-encoding", "connection", "content-length"):
            continue
        if strip_encoding and lower == "accept-encoding":
            continue
        headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    log.debug(f"[HDR] Forwarding headers: {list(headers.keys())}")
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


def _do_background_compression(entry: dict, messages: list, original_hashes: list, auth_headers: dict):
    log.info(
        f"[BG] Starting compression of {len(messages)} messages "
        f"(covers {len(original_hashes)} originals)..."
    )
    try:
        compressed = compressor.compress(messages, auth_headers)
        entry["pending"] = compressed
        entry["pending_hashes"] = original_hashes
        log.info(
            f"[BG] Compression ready: "
            f"~{compressor.estimate_tokens(compressed):,} tokens "
            f"({len(compressed)} messages, covers {len(original_hashes)} originals)"
        )
    except Exception as e:
        log.error(f"[BG] Compression failed: {e}", exc_info=True)
        entry["pending"] = None


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _proxy_raw(self, method: str):
        """Raw proxy — forward request and stream response back."""
        body = self._read_body()
        headers = _forward_headers(self._get_headers_dict(), body if body else None)

        log.info(f"[RAW] {method} {self.path} -> {UPSTREAM_URL} (body={len(body)} bytes)")

        try:
            conn = _upstream_conn()
            conn.request(method, self.path, body=body if body else None, headers=headers)
            resp = conn.getresponse()

            log.info(f"[RAW] Response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[RAW] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            total_bytes = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)

            log.info(f"[RAW] Done streaming {total_bytes:,} bytes")
            conn.close()
        except Exception as e:
            log.error(f"[RAW] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def do_GET(self):
        log.info(f"[REQ] GET {self.path}")
        if self.path == "/health":
            self._handle_health()
        else:
            self._proxy_raw("GET")

    def do_POST(self):
        log.info(f"[REQ] POST {self.path}")
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_raw("POST")

    def do_PUT(self):
        log.info(f"[REQ] PUT {self.path}")
        self._proxy_raw("PUT")

    def do_DELETE(self):
        log.info(f"[REQ] DELETE {self.path}")
        self._proxy_raw("DELETE")

    def do_PATCH(self):
        log.info(f"[REQ] PATCH {self.path}")
        self._proxy_raw("PATCH")

    def do_OPTIONS(self):
        log.info(f"[REQ] OPTIONS {self.path}")
        self._proxy_raw("OPTIONS")

    def _handle_health(self):
        active = sum(
            1 for e in store.entries.values()
            if e["thread"] is not None and e["thread"].is_alive()
        )
        data = {
            "status": "ok",
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "summarizer_model": SUMMARIZER_MODEL,
            "upstream_url": UPSTREAM_URL,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "active_conversations": len(store.entries),
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

        log.info(f"[MSG] POST {self.path} (body={len(raw_body)} bytes)")
        log.debug(f"[MSG] Request headers: {list(req_headers.keys())}")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.error("[MSG] Invalid JSON in request body")
            error_body = b'{"error":"Invalid JSON"}'
            self.send_response(400)
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        model = payload.get("model", "unknown")

        # Hash all messages for content-based matching
        msg_hashes = _hash_messages(messages)
        entry = store.get_or_create(messages)

        estimated = compressor.estimate_tokens(messages)
        token_count = entry["last_input_tokens"] if entry["last_input_tokens"] > 0 else estimated

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} est_tokens=~{estimated:,} "
            f"last_input_tokens={entry['last_input_tokens']:,}"
        )

        # Promote pending compression
        if entry["pending"] is not None:
            entry["prefix"] = entry["pending"]
            entry["original_hashes"] = entry["pending_hashes"]
            entry["pending"] = None
            entry["pending_hashes"] = None
            log.info(
                f"[MSG] New compression ready: {len(entry['prefix'])} prefix messages "
                f"replacing {len(entry['original_hashes'])} original messages"
            )

        # Content-based injection: if we recognize the messages, replace them
        if entry["prefix"] is not None and entry["original_hashes"]:
            oh = entry["original_hashes"]

            if len(oh) <= len(msg_hashes) and oh == msg_hashes[:len(oh)]:
                # Full match — replace compressed messages with prefix
                new_messages = messages[len(oh):]
                merged = entry["prefix"] + new_messages
                merged = _validate_tool_pairs(merged)

                # Strip cache_control from injected messages
                for msg in merged:
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                block.pop("cache_control", None)

                merged_tokens = compressor.estimate_tokens(merged)
                if merged_tokens < token_count:
                    log.info(
                        f"[MSG] Injecting: ~{token_count:,} -> ~{merged_tokens:,} tokens "
                        f"({len(messages)} -> {len(merged)} messages, "
                        f"replaced {len(oh)} with {len(entry['prefix'])} prefix "
                        f"+ {len(new_messages)} new)"
                    )
                    payload["messages"] = merged
                    token_count = merged_tokens
                else:
                    log.info(
                        f"[MSG] Compression no longer helps: "
                        f"merged={merged_tokens:,} >= current={token_count:,}, clearing"
                    )
                    entry["prefix"] = None
                    entry["original_hashes"] = []
            else:
                # Hashes don't match — conversation was compacted or changed
                log.info(
                    f"[MSG] Content mismatch: stored {len(oh)} hashes, "
                    f"request has {len(msg_hashes)} messages. Resetting compression."
                )
                entry["prefix"] = None
                entry["original_hashes"] = []

        # Trigger background compression
        current_messages = payload.get("messages", messages)
        msg_estimate = compressor.estimate_tokens(current_messages)
        already_compressing = entry["thread"] is not None and entry["thread"].is_alive()
        trigger_tokens = entry["last_input_tokens"] if entry["last_input_tokens"] > 0 else token_count

        if trigger_tokens > TRIGGER_TOKENS and msg_estimate > TARGET_TOKENS and not already_compressing:
            log.info(
                f"[MSG] Context at ~{trigger_tokens:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                f"Compressing in background..."
            )
            t = threading.Thread(
                target=_do_background_compression,
                args=(entry, current_messages, msg_hashes, auth_headers),
                daemon=True,
            )
            t.start()
            entry["thread"] = t
        else:
            log.debug(
                f"[MSG] No compression needed: trigger_tokens={trigger_tokens:,} "
                f"trigger={TRIGGER_TOKENS:,} est={msg_estimate:,} compressing={already_compressing}"
            )

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        log.info(f"[MSG] Forwarding to {UPSTREAM_URL}{self.path} ({len(body):,} bytes)")

        try:
            conn = _upstream_conn()
            conn.request("POST", self.path, body=body, headers=headers)
            resp = conn.getresponse()

            log.info(f"[MSG] Upstream response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[MSG] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            log.info(f"[MSG] Streaming response...")

            # Stream response and capture SSE token data
            buffer = b""
            total_bytes = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)
                if is_streaming:
                    buffer += chunk

            log.info(f"[MSG] Done streaming {total_bytes:,} bytes")

            # Extract input tokens from SSE stream
            if is_streaming and buffer:
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
                                entry["last_input_tokens"] = total_input
                                log.info(f"[MSG] Input tokens from SSE: {total_input:,}")
                            break
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse SSE for tokens: {e}")
            elif not is_streaming and buffer:
                try:
                    data = json.loads(buffer)
                    usage = data.get("usage", {})
                    total_input = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    if total_input > 0:
                        entry["last_input_tokens"] = total_input
                        log.info(f"[MSG] Input tokens from response: {total_input:,}")
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for tokens: {e}")

            conn.close()

        except Exception as e:
            log.error(f"[MSG] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)


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
    log.info(f"  Matching: content-based (no sessions/fingerprints)")

    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
