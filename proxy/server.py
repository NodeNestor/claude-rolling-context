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
from collections import OrderedDict
import sys
import logging
import logging.handlers
import threading
import time
import ssl
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import endpoints
import switch
from compressor import RollingCompressor

class FlushRotatingHandler(logging.handlers.RotatingFileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


def _log_setting(name, default):
    """Env first, then settings.json — same precedence as every other knob."""
    return os.environ.get(name) or endpoints.settings_env().get(name) or default


# Was hardcoded DEBUG with an unrotated FileHandler, while start-proxy.sh
# redirected stdout into a second file. Every line landed on disk twice and
# nothing ever pruned either one; a reporter hit 5.9 GB in rolling-context-proxy.log
# on a proxy left up for days (issue #7).
_LEVEL = getattr(logging, str(_log_setting("ROLLING_CONTEXT_LOG_LEVEL", "INFO")).upper(),
                 logging.INFO)
try:
    _MAX_MB = max(1, int(_log_setting("ROLLING_CONTEXT_LOG_MAX_MB", "10")))
except (TypeError, ValueError):
    _MAX_MB = 10
try:
    _BACKUPS = max(0, int(_log_setting("ROLLING_CONTEXT_LOG_BACKUPS", "3")))
except (TypeError, ValueError):
    _BACKUPS = 3

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
# encoding is explicit: without it Windows falls back to cp1252 and the em
# dashes in these log messages land as mojibake.
_log_handler = FlushRotatingHandler(
    _log_path, mode="a", encoding="utf-8",
    maxBytes=_MAX_MB * 1024 * 1024, backupCount=_BACKUPS,
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_log_handler.setLevel(_LEVEL)

# stdout is redirected to rolling-context-proxy.log by the start hook, which
# only truncates on restart — so it must not carry routine traffic or a
# long-lived proxy grows without bound. WARNING and above keeps crashes and
# real problems visible there without duplicating the whole stream.
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_stream_handler.setLevel(max(_LEVEL, logging.WARNING))

logging.basicConfig(level=_LEVEL, handlers=[_stream_handler, _log_handler])
log = logging.getLogger("rolling-context")
log.info(
    f"Logging at {logging.getLevelName(_LEVEL)}; {_log_path} rotates at "
    f"{_MAX_MB} MB x {_BACKUPS}. Set ROLLING_CONTEXT_LOG_LEVEL=DEBUG for detail."
)

LISTEN_PORT = endpoints.LISTEN_PORT

UPSTREAM_URL = endpoints.load_upstream(LISTEN_PORT)
TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER") or "100000")
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET") or "40000")
# Empty = native mode compresses with the session's own model (prompt-cache
# hit); set to pin a specific summarizer model.
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL") or ""
# After a failed compression, wait this long before trying again — otherwise a
# failing summarizer (e.g. rate-limited) gets re-hammered on every request.
# Scoped per conversation: a single global timestamp meant one conversation's
# failure silenced every other conversation on the machine for 5 minutes.
FAILURE_COOLDOWN = int(os.environ.get("ROLLING_CONTEXT_FAILURE_COOLDOWN") or "300")
# Ceiling on concurrent background compactions across all conversations. This
# exists to bound cost and upstream rate-limit pressure, NOT to serialise work:
# before v1.11.4 the limit was effectively 1 and applied globally, so one
# conversation compacting starved every other conversation of a compression
# entry — which is why only ~21% of eligible requests were ever injected (#8).
MAX_CONCURRENT_COMPACTIONS = max(
    1, int(os.environ.get("ROLLING_CONTEXT_MAX_CONCURRENT") or "4"))

ssl_ctx = ssl.create_default_context()
_parsed_upstream = urlparse(UPSTREAM_URL)
UPSTREAM_PATH = _parsed_upstream.path or ""


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
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


# --- per-session toggle (issue #6) ------------------------------------------
# /rolling-context:off prints a marker; slash command output is inserted into
# the transcript inside <local-command-stdout>, so every later request in that
# conversation carries it. Reading the newest marker gives us per-session scope
# without tracking sessions — the same content recognition the proxy already
# runs on. Matching is confined to <local-command-stdout> blocks so that merely
# reading switch.py in a conversation (a tool result, not a command block)
# cannot toggle anything.

_STDOUT_BLOCK_RE = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
)
_SESSION_MARKER_RE = re.compile(r"<<rolling-context:session-(off|on)>>")


def _iter_text(content):
    """Yield the plain-text pieces of a message without serializing it.

    Deliberately not json.dumps — this runs over the whole history on every
    request, and the marker only ever lives in text.
    """
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    yield text
                nested = block.get("content")
                if isinstance(nested, (str, list)):
                    yield from _iter_text(nested)


class SessionToggleStore:
    """Latches each session's toggle against its Claude Code session id.

    The marker only ever appears in the conversation that ran the command, but
    Claude Code sends X-Claude-Code-Session-Id on every request and a subagent
    inherits its parent's session id (subagents get their own transcript, not
    their own session). Latching the marker against that id is what makes
    /rolling-context:off reach the subagents a session spawns.

    It also outlives the marker: if a conversation is /compact'ed and the
    marker is summarized away, the latch still holds.

    Bounded and lossy on purpose. Overflowing or restarting the proxy forgets a
    session, which costs savings for one more request until the marker is seen
    again — never correctness.
    """

    def __init__(self, limit=512):
        self._lock = threading.Lock()
        self._state = OrderedDict()
        self._limit = limit

    def set(self, session_id: str, disabled: bool):
        if not session_id:
            return
        with self._lock:
            previous = self._state.get(session_id)
            self._state[session_id] = disabled
            self._state.move_to_end(session_id)
            while len(self._state) > self._limit:
                self._state.popitem(last=False)
            return previous != disabled

    def get(self, session_id: str):
        if not session_id:
            return None
        with self._lock:
            if session_id not in self._state:
                return None
            self._state.move_to_end(session_id)
            return self._state[session_id]

    def __len__(self):
        return len(self._state)


session_toggles = SessionToggleStore()


def _session_disabled(messages: list):
    """Newest /rolling-context session marker in this conversation.

    Returns True (off), False (on), or None (never set — follow the machine
    setting). Scans newest-first so the last toggle wins.
    """
    for msg in reversed(messages):
        for text in _iter_text(msg.get("content", "")):
            # Cheap reject first: the vast majority of messages never match.
            if "rolling-context:session-" not in text:
                continue
            for block in reversed(_STDOUT_BLOCK_RE.findall(text)):
                found = _SESSION_MARKER_RE.findall(block)
                if found:
                    return found[-1] == "off"
    return None


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
                # DEBUG, not WARNING: a scan miss is the ordinary case, emitted
                # per stored entry per request, and it dumps conversation
                # content to disk. At WARNING it was the single largest
                # contributor to a 5.9 GB log (issue #7) — 56,960 lines against
                # 53 real injections. The isEnabledFor guard means the diff is
                # not even computed unless someone asked for DEBUG.
                if (not found and chain_len <= len(msg_hashes)
                        and log.isEnabledFor(logging.DEBUG)):
                    mismatches = []
                    for i in range(min(chain_len, len(msg_hashes))):
                        if oh[i] != msg_hashes[i]:
                            mismatches.append(i)
                    log.debug(
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
                            log.debug(
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


# Failure timestamps per conversation, not one global clock. The global version
# meant a single rate-limited conversation put every other conversation on the
# machine into a FAILURE_COOLDOWN-long silence (#8).
_compression_failures = {}          # conversation key -> time.time() of failure
_failure_lock = threading.Lock()

# Injection accounting, so "is this thing actually working?" is answerable from
# the log at INFO instead of requiring DEBUG and a transcript scrape (#8).
_stats = {"injected": 0, "missed": 0}
_stats_lock = threading.Lock()


def _conversation_key(session_id: str, msg_hashes: list) -> str:
    """Identify a conversation for concurrency accounting ONLY.

    Never used for matching — the store stays content-addressed, so this cannot
    reintroduce session coupling. Claude Code sends X-Claude-Code-Session-Id;
    when it is absent (older builds, other clients) the first message hash is
    stable for the life of a conversation and is good enough to keep one
    conversation from blocking another.
    """
    if session_id:
        return "sid:" + session_id
    return "msg:" + (msg_hashes[0] if msg_hashes else "empty")


def _note_compression_failure(owner: str):
    now = time.time()
    with _failure_lock:
        _compression_failures[owner] = now
        if len(_compression_failures) > 512:
            cutoff = now - FAILURE_COOLDOWN
            for k in [k for k, v in _compression_failures.items() if v < cutoff]:
                _compression_failures.pop(k, None)


def _cooldown_remaining(owner: str) -> float:
    with _failure_lock:
        failed_at = _compression_failures.get(owner, 0.0)
    return FAILURE_COOLDOWN - (time.time() - failed_at)


# Wall-clock time of the last compression injection — the moment old messages
# actually left the model's context. Exposed at /lean/status so companion
# plugins (nestor-lean) can invalidate "the model already saw this" knowledge.
_last_injection_ts = 0.0


def _do_background_compression(entry: dict, messages: list, auth_headers: dict,
                               real_token_count: int = None, payload: dict = None):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed = compressor.compress(messages, auth_headers,
                                         real_token_count=real_token_count, payload=payload)
        if compressed is None:
            # Nothing worth compressing — don't leave an empty entry behind
            store.remove(entry)
            return
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
        owner = entry.get("owner") or ""
        _note_compression_failure(owner)
        log.error(
            f"[BG] Compression failed (this conversation cools down "
            f"{FAILURE_COOLDOWN}s; others are unaffected): {e}",
            exc_info=True,
        )
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
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health()
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
        elif normalized_path == "/lean/status":
            self._handle_lean_status()
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

    def _handle_lean_status(self):
        """Machine-readable status for companion plugins (nestor-lean).

        last_injection_ts is global across all conversations flowing through
        this proxy — consumers must treat it as a conservative signal (a
        compression in ANY session invalidates, which only costs savings,
        never correctness).
        """
        data = {
            "status": "ok",
            "last_injection_ts": _last_injection_ts,
            "stored_compressions": len(store.compressions),
            "enabled": not switch.is_disabled(),
        }
        body = json.dumps(data).encode()
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
        from compressor import NATIVE_MODE, SUMMARIZER_FORMAT
        data = {
            "status": "ok",
            "enabled": not switch.is_disabled(),
            "default_enabled": switch.config_default_enabled(),
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "summarizer_model": SUMMARIZER_MODEL or "(session model)",
            "summarizer_mode": "native" if NATIVE_MODE else f"flattened/{SUMMARIZER_FORMAT}",
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

        # Claude Code sends this on every request, and a subagent inherits its
        # parent's — it gets its own transcript, not its own session. That is
        # what lets a session's toggle reach the agents it spawns.
        session_id = self.headers.get("X-Claude-Code-Session-Id") or ""

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,} "
            f"session={session_id[:8] or '(none)'}"
        )

        # /rolling-context:off — resolved fresh per request so the toggle is
        # live. Machine-wide off wins; otherwise this conversation's own marker
        # decides, and falls back to on. Disabled means "stop acting", not
        # "forget": stored compressions are left intact so turning back on
        # resumes without recompressing.
        # A marker in this request updates the latch for its session; requests
        # without one (later turns, and the subagents this session spawns) read
        # it back.
        marker_state = _session_disabled(messages)
        if marker_state is not None:
            if session_toggles.set(session_id, marker_state):
                log.info(
                    f"[MSG] Session {session_id[:8] or '(none)'} toggled "
                    f"{'OFF' if marker_state else 'ON'} by marker"
                )

        # Precedence: env kill-switch, then an explicit machine-wide off, then
        # this session's own setting, then the configured default.
        if switch.is_disabled():
            disabled, scope = True, "machine-wide"
        else:
            session_state = marker_state
            scope = "this session"
            if session_state is None:
                session_state = session_toggles.get(session_id)
                scope = "inherited from this session"
            if session_state is None:
                disabled, scope = not switch.config_default_enabled(), "config default"
            else:
                disabled = session_state
        if disabled:
            log.info(
                f"[MSG] rolling-context is OFF ({scope}) — passing through "
                f"untouched ({len(store.compressions)} compression(s) kept for later)"
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
        # Skipped entirely while off — find_match is also the only caller that
        # prunes no-longer-helpful entries, so not running it keeps the store
        # exactly as it was.
        match, match_end = (None, -1) if disabled else store.find_match(msg_hashes, messages)
        injected = False

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]

            # Strip cache_control from injected prefix messages ONLY.
            # The verbatim tail keeps Claude Code's cache_control breakpoints —
            # stripping those disabled prompt caching entirely, so every request
            # after the first injection paid full input-token cost (issue #1/#4).
            for msg in match["prefix"]:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

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
                global _last_injection_ts
                _last_injection_ts = time.time()
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

            # Injection accounting (#8). Whether the proxy is actually doing its
            # job should be answerable from INFO logs, not by correlating
            # session transcripts against DEBUG output.
            if not disabled:
                with _stats_lock:
                    if injected:
                        _stats["injected"] += 1
                    elif total_input > TRIGGER_TOKENS:
                        _stats["missed"] += 1
                    seen = _stats["injected"] + _stats["missed"]
                    if seen and seen % 25 == 0:
                        log.info(
                            f"[STATS] prefix injected on {_stats['injected']}/{seen} "
                            f"requests at or over the trigger "
                            f"({_stats['injected'] / seen * 100:.0f}%)"
                        )

            # Trigger compression based on token count. The minimum message
            # count keeps us from "compressing" sessions whose bulk is the
            # system prompt / first-message context, which we can't remove.
            if disabled:
                if total_input > TRIGGER_TOKENS:
                    log.info(
                        f"[MSG] {total_input:,} tokens is over trigger, but "
                        f"rolling-context is OFF — not compressing"
                    )
            elif total_input > 0 and total_input > TRIGGER_TOKENS and len(current_messages) >= 6:
                convo_key = _conversation_key(session_id, msg_hashes)
                # Per conversation, not process-global. The old check scanned
                # every entry in the store, so ANY conversation compacting
                # blocked ALL others — and did so via a bare `pass`, with no log
                # line, which is why it never showed up in anyone's logs. Most
                # conversations never won the race, never got a compression
                # entry, and so never had anything to inject on later turns (#8).
                mine_compressing = any(
                    e["thread"] is not None and e["thread"].is_alive()
                    and e.get("owner") == convo_key
                    for e in store.compressions
                )
                in_flight = sum(
                    1 for e in store.compressions
                    if e["thread"] is not None and e["thread"].is_alive()
                )
                # A conversation with no usable compression yet is paying full
                # context cost on every single turn, and it is precisely the one
                # a busy machine starves: it always arrives to find the slots
                # taken. So the cap applies to REFRESHES only, and cold starts
                # are admitted up to a hard ceiling. Making the cap absolute
                # reintroduced #8 in a new form — measured: with 5 other
                # conversations running, the sixth never compressed at all.
                has_usable = any(
                    e.get("owner") == convo_key and e.get("prefix") is not None
                    for e in store.compressions
                )
                cooldown_left = _cooldown_remaining(convo_key)
                if mine_compressing:
                    log.info(
                        "[MSG] Over trigger, but this conversation is already "
                        "compressing — skipping"
                    )
                elif in_flight >= MAX_CONCURRENT_COMPACTIONS * 2:
                    log.info(
                        f"[MSG] Over trigger, but {in_flight} compactions are in "
                        f"flight (hard ceiling {MAX_CONCURRENT_COMPACTIONS * 2}) "
                        f"— skipping. Raise ROLLING_CONTEXT_MAX_CONCURRENT if "
                        f"this is frequent."
                    )
                elif in_flight >= MAX_CONCURRENT_COMPACTIONS and has_usable:
                    log.info(
                        f"[MSG] Over trigger, but {in_flight} compactions are in "
                        f"flight (cap {MAX_CONCURRENT_COMPACTIONS}) and this "
                        f"conversation already has a summary — deferring refresh"
                    )
                elif cooldown_left > 0:
                    log.info(
                        f"[MSG] Over trigger but this conversation's last "
                        f"compression failed — cooling down another {cooldown_left:.0f}s"
                    )
                else:
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                        f"Compressing in background..."
                    )
                    entry = store.add()
                    entry["owner"] = convo_key
                    t = threading.Thread(
                        target=_do_background_compression,
                        args=(entry, current_messages, auth_headers),
                        kwargs={"real_token_count": total_input, "payload": payload},
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
    from compressor import (NATIVE_MODE, NATIVE_FALLBACK, SUMMARIZER_BASE_URL,
                            SUMMARIZER_FORMAT)
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL or '(session model)'}")
    log.info(f"  Summarizer mode: "
             f"{'native (cloned session request, prompt-cached)' if NATIVE_MODE else f'flattened/{SUMMARIZER_FORMAT}'}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")
    # Printed separately from the upstream on purpose: when these two disagree
    # unintentionally, compaction 401s forever and nothing ever compresses.
    log.info(f"  Compacting via: {SUMMARIZER_BASE_URL}"
             f"{' (third-party — flattened fallback armed)' if NATIVE_FALLBACK else ''}")
    log.info(f"  Matching: content-based (no sessions/fingerprints)")

    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
