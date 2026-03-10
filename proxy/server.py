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

UPSTREAM_URL = os.environ.get("ROLLING_CONTEXT_UPSTREAM") or "https://api.anthropic.com"
LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT") or "5588")
TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER") or "100000")
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET") or "40000")
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL") or "claude-haiku-4-5-20251001"

ssl_ctx = ssl.create_default_context()
_parsed_upstream = urlparse(UPSTREAM_URL)
# Preserve upstream path (e.g. "/api/anthropic" from "https://proxy.example.com/api/anthropic")
UPSTREAM_PATH = _parsed_upstream.path or ""


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
    # Ensure no double slashes
    if upstream_path.endswith("/") and request_path.startswith("/"):
        return upstream_path[:-1] + request_path
    if not upstream_path.endswith("/") and not request_path.startswith("/"):
        return upstream_path + "/" + request_path
    return upstream_path + request_path

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

import re

_VOLATILE_TAGS_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|local-command-stdout|"
    r"available-deferred-tools)>.*?</(?:system-reminder|local-command-caveat|"
    r"local-command-stdout|available-deferred-tools)>",
    re.DOTALL,
)


def _strip_volatile_tags(text: str) -> str:
    """Strip Claude Code's dynamic tags that change between requests."""
    return _VOLATILE_TAGS_RE.sub("", text)


def _normalize_content(content):
    """Strip volatile metadata (cache_control, system-reminder) for stable hashing."""
    if isinstance(content, str):
        return _strip_volatile_tags(content)
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
                    elif k == "text" and isinstance(v, str):
                        b[k] = _strip_volatile_tags(v)
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
    """Content-based compression tracking. No sessions, no fingerprints, no keys.

    Stores a list of compressions. Each has original_hashes (what was compressed)
    and prefix (the replacement). On ANY request, scans messages — if the hashes
    match a stored compression, replaces them with the prefix.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._compressions = []  # list of compression entries

    def find_match(self, msg_hashes: list, messages: list = None):
        """Find a compression whose hash chain appears in msg_hashes.

        Returns the match whose chain ends furthest into the request
        (latest compression = covers the most history).
        Replaces everything up to and including the match, since the
        compression already contains a summary of everything before it.
        """
        with self._lock:
            best = None
            best_end = -1  # position in msg_hashes where the match ends
            for entry in self._compressions:
                oh = entry["original_hashes"]
                if not oh:
                    continue
                # Search for the hash chain in msg_hashes
                chain_len = len(oh)
                found = False
                for start in range(len(msg_hashes) - chain_len + 1):
                    if msg_hashes[start:start + chain_len] == oh:
                        end = start + chain_len
                        if end > best_end:
                            best = entry
                            best_end = end
                        found = True
                        break
                if not found and chain_len <= len(msg_hashes):
                    # Count total mismatches
                    mismatches = []
                    for i in range(min(chain_len, len(msg_hashes))):
                        if oh[i] != msg_hashes[i]:
                            mismatches.append(i)
                    log.warning(
                        f"[MATCH] No match: chain={chain_len} req={len(msg_hashes)} "
                        f"mismatches={len(mismatches)} at positions: "
                        f"{mismatches[:10]}{'...' if len(mismatches) > 10 else ''}"
                    )
                    # Dump content of first mismatched message for debugging
                    if mismatches and messages and entry.get("_debug_messages"):
                        idx = mismatches[0]
                        stored_msg = entry["_debug_messages"][idx] if idx < len(entry["_debug_messages"]) else None
                        incoming_msg = messages[idx] if idx < len(messages) else None
                        if stored_msg and incoming_msg:
                            s_content = str(stored_msg.get("content", ""))[:500]
                            i_content = str(incoming_msg.get("content", ""))[:500]
                            log.warning(
                                f"[MATCH] Mismatch at [{idx}] role={stored_msg.get('role')}:\n"
                                f"  STORED:   {s_content}\n"
                                f"  INCOMING: {i_content}"
                            )
            return best, best_end

    def add(self) -> dict:
        entry = {
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "thread": None,          # background compression thread
        }
        with self._lock:
            self._compressions.append(entry)
        return entry

    def remove(self, entry: dict):
        with self._lock:
            self._compressions = [e for e in self._compressions if e is not entry]

    @property
    def compressions(self):
        return self._compressions


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


def _do_background_compression(entry: dict, messages: list, auth_headers: dict, real_token_count: int = None):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed = compressor.compress(messages, auth_headers, real_token_count=real_token_count)
        # compressed = [summary, ack] + recent_verbatim
        # Prefix = ONLY [summary, ack] — verbatim messages come from the
        # original request during injection, so including them in the prefix
        # would cause duplication.
        prefix = compressed[:2]
        # Key = the messages that were summarized away (not the verbatim ones).
        recent_count = len(compressed) - 2  # subtract summary + ack
        summarized = messages[:len(messages) - recent_count]
        # Skip old summary prefix if present
        from compressor import SUMMARY_MARKER
        start = 0
        if summarized and isinstance(summarized[0].get("content", ""), str):
            if SUMMARY_MARKER in summarized[0]["content"]:
                start = 2
        key_hashes = _hash_messages(summarized[start:])
        entry["pending"] = prefix
        entry["pending_hashes"] = key_hashes
        entry["_debug_messages"] = summarized[start:]  # for mismatch debugging
        log.info(
            f"[BG] Compression ready: "
            f"{compressor._count_chars(prefix):,} chars "
            f"({len(prefix)} prefix messages, key={len(key_hashes)} hashes, "
            f"summarized {len(summarized) - start} messages)"
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
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request(method, upstream_full_path, body=body if body else None, headers=headers)
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
        # Parse path to handle query strings and prevent path traversal
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health()
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
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

    def _handle_debug_compressions(self):
        entries = []
        for i, entry in enumerate(store.compressions):
            info = {
                "index": i,
                "hash_chain_length": len(entry.get("original_hashes") or []),
                "has_prefix": entry["prefix"] is not None,
                "prefix_content": None,
            }
            if entry["prefix"]:
                for msg in entry["prefix"]:
                    content = msg.get("content", "")
                    if isinstance(content, str) and "[ROLLING_CONTEXT_SUMMARY]" in content:
                        info["prefix_content"] = content
            entries.append(info)
        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        active = sum(
            1 for e in store.compressions
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
            "stored_compressions": len(store.compressions),
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
        msg_chars = compressor._count_chars(messages)

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,}"
        )

        # Promote any pending compressions
        for entry in store.compressions:
            if entry["pending"] is not None:
                entry["prefix"] = entry["pending"]
                entry["original_hashes"] = entry["pending_hashes"]
                entry["pending"] = None
                entry["pending_hashes"] = None
                log.info(
                    f"[MSG] Compression promoted: {len(entry['prefix'])} prefix messages "
                    f"replacing {len(entry['original_hashes'])} originals"
                )

        # Scan: do any stored compressions match this request's messages?
        match, match_end = store.find_match(msg_hashes, messages)
        injected = False

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]
            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

            # Strip cache_control from injected messages
            for msg in merged:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

            merged_chars = compressor._count_chars(merged)
            if merged_chars < msg_chars:
                log.info(
                    f"[MSG] Injecting: {msg_chars:,} -> {merged_chars:,} chars "
                    f"({len(messages)} -> {len(merged)} messages, "
                    f"replaced 0-{match_end} with {len(match['prefix'])} prefix "
                    f"+ {len(new_messages)} new)"
                )
                payload["messages"] = merged
                msg_chars = merged_chars
                injected = True
            else:
                log.info(
                    f"[MSG] Compression no longer helps: "
                    f"merged={merged_chars:,} >= current={msg_chars:,} chars, removing"
                )
                store.remove(match)
                match = None

        # Save current state for post-response compression trigger
        current_messages = payload.get("messages", messages)

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        log.info(f"[MSG] Forwarding to {UPSTREAM_URL}{self.path} ({len(body):,} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request("POST", upstream_full_path, body=body, headers=headers)
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
            total_input = 0
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
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        evt_type = data.get("type", "")

                        # Anthropic native: usage in message_start.message.usage
                        if evt_type == "message_start":
                            usage = data.get("message", {}).get("usage", {})
                            tokens = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                            )
                            if tokens > 0:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_start: {total_input:,}")

                        # Proxy/converter: usage in message_delta.usage (e.g. CodeGate)
                        elif evt_type == "message_delta":
                            usage = data.get("usage", {})
                            tokens = int(usage.get("input_tokens", 0))
                            if tokens > 0 and tokens > total_input:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_delta: {total_input:,}")

                    if total_input == 0:
                        sse_lines = [l for l in text.split("\n") if l.startswith("data: ")]
                        log.warning(
                            f"[MSG] No input tokens found in SSE! "
                            f"Total events: {len(sse_lines)}"
                        )
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
                        log.info(f"[MSG] Input tokens from response: {total_input:,}")
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for tokens: {e}")

            conn.close()

            # Fallback: estimate tokens from chars if SSE didn't provide usage
            if total_input == 0 and msg_chars > 0:
                total_input = msg_chars // 4  # rough chars-to-tokens estimate
                log.info(
                    f"[MSG] No tokens from SSE, estimating from chars: "
                    f"{msg_chars:,} chars -> ~{total_input:,} tokens"
                )

            # Trigger compression based on token count
            if total_input > 0 and total_input > TRIGGER_TOKENS:
                already_compressing = any(
                    e["thread"] is not None and e["thread"].is_alive()
                    for e in store.compressions
                )
                if not already_compressing:
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                        f"Compressing in background..."
                    )
                    entry = store.add()
                    t = threading.Thread(
                        target=_do_background_compression,
                        args=(entry, current_messages, auth_headers),
                        kwargs={"real_token_count": total_input},
                        daemon=True,
                    )
                    t.start()
                    entry["thread"] = t

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
