"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.
Handles multiple concurrent Claude Code sessions safely.
"""

import asyncio
import hashlib
import json
import os
import sys
import logging

import aiohttp
from aiohttp import web

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

compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
)


class SessionTracker:
    """Tracks compression state per session. Sessions are identified by
    a fingerprint of the first user message (unique per conversation)."""

    def __init__(self):
        self._sessions = {}  # fingerprint -> {pending, pending_msg_count, task, last_input_tokens}

    def _fingerprint(self, messages: list) -> str:
        """Hash the first user message content to identify the session."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content)
                return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        return "unknown"

    def get(self, messages: list) -> dict:
        fp = self._fingerprint(messages)
        if fp not in self._sessions:
            self._sessions[fp] = {
                "pending": None,          # Compressed messages ready to apply
                "pending_msg_count": 0,   # How many messages were in the array when compression started
                "task": None,             # Running asyncio compression task
                "last_input_tokens": 0,   # From last API response
            }
        return self._sessions[fp]

    def cleanup_stale(self, max_sessions: int = 50):
        """Remove oldest sessions if we have too many."""
        if len(self._sessions) > max_sessions:
            # Remove sessions with no pending work
            to_remove = []
            for fp, state in self._sessions.items():
                if state["pending"] is None and (state["task"] is None or state["task"].done()):
                    to_remove.append(fp)
            for fp in to_remove[:len(self._sessions) - max_sessions]:
                del self._sessions[fp]


tracker = SessionTracker()


def _forward_headers(request: web.Request, body: bytes = None) -> dict:
    headers = {}
    for key, value in request.headers.items():
        if key.lower() != "host":
            headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    return headers


def get_passthrough_headers(request: web.Request) -> dict:
    """Capture all headers from the request to reuse for summarization calls.
    Same auth, same everything — just like a normal passthrough."""
    headers = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


async def _do_background_compression(session_state: dict, messages: list, msg_count: int, auth_headers: dict):
    """Run compression in background. Stores result in session state."""
    try:
        compressed = await compressor.compress(messages, auth_headers)
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


async def proxy_streaming(request: web.Request, target_url: str, body: bytes,
                          headers: dict, session_state: dict) -> web.StreamResponse:
    """Forward streaming request. Parse SSE to extract usage from final event."""
    response = web.StreamResponse()

    async with aiohttp.ClientSession() as session:
        async with session.post(target_url, data=body, headers=headers) as upstream:
            response.set_status(upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    response.headers[key] = value
            await response.prepare(request)

            buffer = b""
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                buffer += chunk

            # Extract input_tokens from the message_start SSE event
            # With prompt caching (beta=true), total = input_tokens + cache_creation + cache_read
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
                            log.debug(f"Tracked {total_input:,} input tokens from stream")
                        break
            except Exception:
                pass

    await response.write_eof()
    return response


async def proxy_non_streaming(request: web.Request, target_url: str, body: bytes,
                              headers: dict, session_state: dict) -> web.Response:
    async with aiohttp.ClientSession() as session:
        async with session.post(target_url, data=body, headers=headers) as upstream:
            resp_body = await upstream.read()

            # Extract input_tokens from response (handle prompt caching)
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

            return web.Response(
                status=upstream.status,
                body=resp_body,
                content_type=upstream.content_type,
            )


async def handle_messages(request: web.Request) -> web.StreamResponse:
    raw_body = await request.read()
    auth_headers = get_passthrough_headers(request)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    messages = payload.get("messages", [])
    is_streaming = payload.get("stream", False)
    session_state = tracker.get(messages)

    # Use the accurate token count from last API response if available,
    # otherwise fall back to our rough estimate
    estimated = compressor.estimate_tokens(messages)
    if session_state["last_input_tokens"] > 0:
        token_count = session_state["last_input_tokens"]
    else:
        token_count = estimated
    log.debug(f"Token count: ~{token_count:,}, estimate={estimated:,}, messages={len(messages)}")

    # Apply pending compression if available
    if session_state["pending"] is not None:
        compressed = session_state["pending"]
        original_count = session_state["pending_msg_count"]
        session_state["pending"] = None
        session_state["pending_msg_count"] = 0

        # Append any messages that arrived after compression was triggered
        new_messages = messages[original_count:] if original_count < len(messages) else []
        merged = compressed + new_messages
        merged_tokens = compressor.estimate_tokens(merged)

        if merged_tokens < token_count:
            log.info(
                f"Applying compression: ~{token_count:,} -> ~{merged_tokens:,} tokens "
                f"({len(messages)} -> {len(merged)} messages, "
                f"{len(new_messages)} new messages appended)"
            )
            payload["messages"] = merged
            token_count = merged_tokens
            # Reset tracked tokens since we changed the messages
            session_state["last_input_tokens"] = 0

    # Trigger background compression if over threshold
    # Recalculate estimate on current payload (may have been compressed above)
    current_messages = payload.get("messages", messages)
    msg_estimate = compressor.estimate_tokens(current_messages)
    already_compressing = (
        session_state["task"] is not None
        and not session_state["task"].done()
    )
    if token_count > TRIGGER_TOKENS and msg_estimate > TARGET_TOKENS and not already_compressing:
        log.info(
            f"Context at ~{token_count:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
            f"Compressing in background..."
        )
        session_state["task"] = asyncio.create_task(
            _do_background_compression(
                session_state, current_messages, len(current_messages), auth_headers
            )
        )

    # Forward immediately — never block
    body = json.dumps(payload).encode("utf-8")
    headers = _forward_headers(request, body)
    target_url = f"{UPSTREAM_URL}{request.path_qs}"

    tracker.cleanup_stale()

    if is_streaming:
        return await proxy_streaming(request, target_url, body, headers, session_state)
    else:
        return await proxy_non_streaming(request, target_url, body, headers, session_state)


async def handle_passthrough(request: web.Request) -> web.StreamResponse:
    raw_body = await request.read()
    headers = _forward_headers(request)
    target_url = f"{UPSTREAM_URL}{request.path_qs}"

    async with aiohttp.ClientSession() as session:
        async with session.request(
            request.method, target_url, data=raw_body, headers=headers
        ) as upstream:
            resp_body = await upstream.read()
            return web.Response(
                status=upstream.status,
                body=resp_body,
                content_type=upstream.content_type,
            )


async def handle_health(request: web.Request) -> web.Response:
    active = sum(
        1 for s in tracker._sessions.values()
        if s["task"] is not None and not s["task"].done()
    )
    return web.json_response({
        "status": "ok",
        "trigger_tokens": TRIGGER_TOKENS,
        "target_tokens": TARGET_TOKENS,
        "summarizer_model": SUMMARIZER_MODEL,
        "upstream_url": UPSTREAM_URL,
        "compression_count": compressor.compression_count,
        "total_tokens_saved": compressor.total_tokens_saved,
        "active_sessions": len(tracker._sessions),
        "active_compressions": active,
    })


def create_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_route("*", "/{path:.*}", handle_passthrough)
    return app


def main():
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")
    app = create_app()
    web.run_app(app, host="127.0.0.1", port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
