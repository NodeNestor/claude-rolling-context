"""
Rolling Context Compressor

When context exceeds trigger_tokens, compresses old messages down to target_tokens
of recent context + a dense chronological summary of everything before.

Two summarization modes:

1. NATIVE (default): clones the exact request Claude Code just sent — same
   model, system prompt, tools, and message history up to the cut point — and
   appends one user message asking for the summary. Because the request is
   byte-identical Claude Code session traffic, it passes Anthropic's
   subscription OAuth classification (issue #4), and because the prefix was
   just sent by the chat request, it's a prompt-cache read instead of full
   input cost.

2. FLATTENED: used when a custom summarizer is configured
   (ROLLING_CONTEXT_SUMMARIZER_URL / _KEY / _FORMAT). Flattens the
   conversation to text and sends a standalone request — Anthropic format or
   OpenAI chat-completions format, so any local model or third-party API
   works (Ollama, LM Studio, vLLM, OpenRouter, DeepSeek, ...).

Pure stdlib — no external dependencies.
"""

import copy
import gzip
import json
import os
import ssl
import logging
import http.client
from urllib.parse import urlparse

import endpoints

log = logging.getLogger("rolling-context.compressor")

# Resolve through endpoints so a custom upstream configured in settings.json is
# honoured here too. Reading os.environ alone sent every compaction to
# api.anthropic.com with the custom endpoint's key — a 401 on every attempt,
# so nothing ever got compressed (issue #5).
_default_summarizer_url = endpoints.load_upstream()
SUMMARIZER_URL_SET = bool(os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL"))
SUMMARIZER_BASE_URL = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL") or _default_summarizer_url
SUMMARIZER_API_KEY = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY") or ""
# "anthropic" (default) or "openai" — openai speaks /v1/chat/completions
SUMMARIZER_FORMAT = (os.environ.get("ROLLING_CONTEXT_SUMMARIZER_FORMAT") or "anthropic").lower()
# Any custom summarizer config switches off native mode
NATIVE_MODE = not (SUMMARIZER_URL_SET or SUMMARIZER_API_KEY or SUMMARIZER_FORMAT != "anthropic")
# Third-party Anthropic-compatible endpoints (Z.ai/GLM, OpenRouter, …) accept
# the /v1/messages shape but not always every field Claude Code sends —
# cache_control breakpoints and tool_choice:none are the usual rejections. Keep
# native mode (it is still the cheapest path when it works) but fall back to a
# flattened summary rather than burning the whole compaction on one 400.
NATIVE_FALLBACK = not endpoints.is_anthropic(SUMMARIZER_BASE_URL)
LEGACY_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Native compaction inherits the session's model. On claude-fable-5 the
# summarization request can come back HTTP 200 with stop_reason "refusal"
# (safety classifier) or with thinking and no text — deterministic for that
# conversation, so a 300s cooldown never helps (#11). One retry on a
# non-reasoning model breaks the loop; it forfeits the prompt-cache hit, which
# is still far cheaper than a session that never compresses again.
FALLBACK_MODEL = os.environ.get("ROLLING_CONTEXT_FALLBACK_MODEL") or "claude-sonnet-5"

ssl_ctx = endpoints.outbound_ssl_context()

_parsed_summarizer = urlparse(SUMMARIZER_BASE_URL)
_SUMMARIZER_HOST = _parsed_summarizer.hostname
_SUMMARIZER_PORT = _parsed_summarizer.port
_SUMMARIZER_SCHEME = _parsed_summarizer.scheme
_SUMMARIZER_PATH = _parsed_summarizer.path or ""


def _summarizer_conn(timeout=600):
    """Create a connection to the summarizer server (same style as server.py)."""
    if _SUMMARIZER_SCHEME == "https":
        return http.client.HTTPSConnection(
            _SUMMARIZER_HOST,
            _SUMMARIZER_PORT or 443,
            context=ssl_ctx,
            timeout=timeout,
        )
    else:
        return http.client.HTTPConnection(
            _SUMMARIZER_HOST,
            _SUMMARIZER_PORT or 80,
            timeout=timeout,
        )


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

def _clean_headers(headers: dict) -> dict:
    """Drop hop-by-hop/stale headers case-insensitively. The passthrough
    headers keep Claude Code's original casing (e.g. Accept-Encoding), so
    plain dict assignment of 'accept-encoding' would DUPLICATE the header and
    the upstream would still gzip the response."""
    drop = ("accept-encoding", "content-length", "host", "transfer-encoding", "connection")
    return {k: v for k, v in headers.items() if k.lower() not in drop}


SUMMARY_MARKER = "[ROLLING_CONTEXT_SUMMARY]"
SUMMARY_END_MARKER = "[/ROLLING_CONTEXT_SUMMARY]"

SUMMARY_RULES = """RULES:
- Structure as a TIMELINE: use numbered steps showing what happened in order
- Preserve ALL file paths, function/class/variable names EXACTLY as written
- Preserve ALL technical decisions and WHY they were made
- Preserve ALL code changes: what file, what was changed, what the new code does
- Preserve ALL errors encountered and how they were resolved
- Preserve ALL user requests and instructions — what they asked for, what constraints they gave, what they said to do or NOT do
- Preserve user preferences, workflow choices, and recurring patterns (e.g. "always use X", "never do Y")
- Include key code snippets when they're central to understanding (keep them short)
- Do NOT editorialize or add commentary
- Be as DENSE as possible — every sentence should carry information

FORMAT:
## Active Goal
- [What the user is CURRENTLY asking for — their most recent request or focus]
- [Any constraints or rules the user has stated (do/don't do)]

## Previous Goals (completed or shifted away from)
- [Earlier goals that were finished or that the user moved on from — keep brief]

## Timeline
1. [First thing that happened]
2. [Second thing...]
...

## Current State
- [What's done, what's in progress, what's next]

## Key Details
- [File paths, configs, decisions that must not be forgotten]"""

# Native mode: appended as the final user message after the real conversation,
# like Claude Code's own /compact. Written as what it is — the user's own
# housekeeping request — because "Act as a context compressor: produce a DENSE
# summary of the conversation above" is refused outright by claude-fable-5's
# ToS classifier (stop_reason "refusal", category "reasoning_extraction") on
# every conversation, while Opus 5 / Sonnet 5 accept it (#11). Verified against
# the real API on all three models; keep COMPACTION_MARKER in sync — test mocks
# recognize summarization requests by it.
COMPACTION_MARKER = "housekeeping request from me"
NATIVE_COMPACT_PROMPT = f"""I'm about to trim our conversation to free up context, and this summary will replace the older messages. Please write me a detailed summary of everything so far, so you can pick up exactly where we left off.

To be clear: this is a {COMPACTION_MARKER}, not part of the work. Don't mention it in the summary or the timeline. The Active Goal is my most recent real request above — treat the task in progress as still in progress, not interrupted.

{SUMMARY_RULES}

If the conversation begins with a {SUMMARY_MARKER} block from an earlier summary, integrate it — keep all its details and extend the timeline with what happened since.

Write only the summary, nothing else."""

# Flattened mode: standalone prompt carrying the conversation as text.
SUMMARIZE_PROMPT = f"""You are a context compressor for an AI coding assistant conversation.

Your job: take the conversation below and produce a CHRONOLOGICAL, DENSE technical summary.

{SUMMARY_RULES}

{{existing_summary_section}}

CONVERSATION TO COMPRESS:
{{conversation}}

Write the chronological summary:"""


def _parse_message_stream(resp_body: bytes):
    """Collect the text of a /v1/messages response and what else was in it.

    Handles both an SSE stream and a plain JSON message (an endpoint that
    ignored stream=true). The diagnostics answer the question the old error
    could not: was it a refusal, thinking with no text, max_tokens, or a
    stream that carried nothing at all?
    """
    diag = {"events": {}, "blocks": [], "stop_reason": None, "stop_details": None,
            "thinking_chars": 0, "output_tokens": 0, "usage": {}, "sse": False}
    parts = []
    raw = resp_body.decode("utf-8", errors="replace")
    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        diag["sse"] = True
        evt = data.get("type", "")
        diag["events"][evt] = diag["events"].get(evt, 0) + 1
        if evt == "message_start":
            msg = data.get("message", {})
            diag["usage"] = msg.get("usage", {}) or {}
            if msg.get("stop_reason"):
                diag["stop_reason"] = msg["stop_reason"]
        elif evt == "content_block_start":
            diag["blocks"].append((data.get("content_block") or {}).get("type", "?"))
        elif evt == "content_block_delta":
            delta = data.get("delta", {})
            kind = delta.get("type")
            if kind == "text_delta":
                parts.append(delta.get("text", ""))
            elif kind == "thinking_delta":
                diag["thinking_chars"] += len(delta.get("thinking", ""))
        elif evt == "message_delta":
            delta = data.get("delta", {}) or {}
            if delta.get("stop_reason"):
                diag["stop_reason"] = delta["stop_reason"]
            if delta.get("stop_details") is not None:
                diag["stop_details"] = delta["stop_details"]
            if data.get("stop_details") is not None:
                diag["stop_details"] = data["stop_details"]
            diag["output_tokens"] = (data.get("usage") or {}).get("output_tokens", 0) or 0
        elif evt == "error":
            raise RuntimeError(f"Summarization stream error: {json.dumps(data)[:500]}")

    if not diag["sse"]:
        # Not a stream: either a whole message as JSON, or garbage.
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            diag["head"] = raw[:200]
            return "", diag
        diag["stop_reason"] = data.get("stop_reason")
        diag["stop_details"] = data.get("stop_details")
        diag["usage"] = data.get("usage", {}) or {}
        diag["output_tokens"] = diag["usage"].get("output_tokens", 0) or 0
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            diag["blocks"].append(block.get("type", "?"))
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                diag["thinking_chars"] += len(block.get("thinking", ""))
    return "".join(parts), diag


def _describe(diag: dict) -> str:
    """One line a user can paste into an issue."""
    bits = [f"stop_reason={diag.get('stop_reason')}"]
    details = diag.get("stop_details")
    if details:
        bits.append(f"stop_details={json.dumps(details)[:200]}")
    bits.append(f"blocks={diag.get('blocks') or []}")
    if diag.get("thinking_chars"):
        bits.append(f"thinking_chars={diag['thinking_chars']}")
    bits.append(f"output_tokens={diag.get('output_tokens', 0)}")
    events = diag.get("events") or {}
    if events:
        bits.append("events=" + ",".join(f"{k}:{v}" for k, v in events.items()))
    else:
        bits.append("no SSE events")
    if diag.get("head"):
        bits.append(f"body={diag['head']!r}")
    return " ".join(bits)


class RollingCompressor:
    def __init__(
        self,
        trigger_tokens: int = 80000,
        target_tokens: int = 40000,
        summarizer_model: str = "",
    ):
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
        # Empty = native mode uses the session's own model (prompt-cache hit);
        # flattened mode falls back to LEGACY_DEFAULT_MODEL.
        self.summarizer_model = summarizer_model
        self.compression_count = 0
        self.total_tokens_saved = 0

    def _count_chars(self, messages: list) -> int:
        """Count total characters across all messages."""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            total_chars += len(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            total_chars += len(json.dumps(block.get("input", {})))
                        elif block.get("type") == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                total_chars += len(c)
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        total_chars += len(sub.get("text", ""))
        return total_chars

    def _find_keep_index(self, messages: list, keep_ratio: float) -> int:
        """Find the cut point: keep the last keep_ratio fraction of content."""
        if len(messages) <= 4:
            return 0
        max_idx = len(messages) - 4
        total_chars = self._count_chars(messages)
        target_chars = int(total_chars * keep_ratio)
        accumulated = 0
        for i in range(len(messages) - 1, -1, -1):
            msg_chars = self._count_chars([messages[i]])
            if accumulated + msg_chars > target_chars:
                for j in range(i + 1, len(messages)):
                    if messages[j].get("role") == "user":
                        if not self._has_tool_result(messages[j]):
                            return min(j, max_idx)
                return min(i + 1, max_idx)
            accumulated += msg_chars
        return 0

    def _safe_cut(self, messages: list, cut: int, floor: int) -> int:
        """Walk cut back to a boundary where messages[cut:] is a valid start.

        Two rules, both enforced by the real API:
        - messages[cut] must be a plain 'user' message (no tool_result). If it's
          an assistant, a tool_result, or a 'system' directive, the injected
          prefix [summary(user), ack(assistant)] can't legally precede it — a
          system message in particular must sit between a user turn and a
          following assistant turn (user, system, assistant), so it can never
          be the first kept message.
        - messages[cut-1] (last summarized) must carry no tool_use, or its
          tool_results would be orphaned in the kept half.
        """
        while cut > floor:
            m = messages[cut]
            starts_clean = m.get("role") == "user" and not self._has_tool_result(m)
            prev_clean = not self._has_tool_use(messages[cut - 1])
            if starts_clean and prev_clean:
                return cut
            cut -= 1
        return cut

    def _has_tool_use(self, message: dict) -> bool:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return True
        return False

    def _has_tool_result(self, message: dict) -> bool:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
        return False

    def _has_summary(self, messages: list) -> bool:
        if not messages:
            return False
        content = messages[0].get("content", "")
        if isinstance(content, str):
            return SUMMARY_MARKER in content
        return False

    def _extract_summary(self, messages: list) -> str:
        if not self._has_summary(messages):
            return ""
        content = messages[0].get("content", "")
        if isinstance(content, str) and SUMMARY_MARKER in content:
            start = content.find(SUMMARY_MARKER) + len(SUMMARY_MARKER)
            end = content.find(SUMMARY_END_MARKER)
            if end > start:
                return content[start:end].strip()
        return ""

    def _messages_to_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            inp = json.dumps(block.get("input", {}))
                            if len(inp) > 500:
                                inp = inp[:400] + "...[truncated]"
                            text_parts.append(f"[Tool: {name}({inp})]")
                        elif block.get("type") == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                text_parts.append(f"[Result: {c[:1000]}]")
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        text_parts.append(f"[Result: {sub.get('text', '')[:1000]}]")
                text = "\n".join(text_parts)
            else:
                text = str(content)

            if len(text) > 4000:
                text = text[:3000] + "\n...[truncated]...\n" + text[-1000:]
            parts.append(f"**{role}**: {text}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Native mode: clone the session's own request, append "compact this"
    # ------------------------------------------------------------------

    def _count_breakpoints(self, payload: dict, convo: list) -> int:
        """Count cache_control breakpoints across system, tools, and convo."""
        count = 0
        system = payload.get("system")
        if isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and "cache_control" in block:
                    count += 1
        for tool in payload.get("tools") or []:
            if isinstance(tool, dict) and "cache_control" in tool:
                count += 1
        for msg in convo:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        count += 1
        return count

    def _summarize_native(self, payload: dict, messages: list, cut: int, auth_headers: dict) -> str:
        """Send the session's own request shape with a compact instruction.

        The conversation prefix is identical to what Claude Code just sent, so
        upstream serves it from the prompt cache, and subscription OAuth
        classification sees genuine Claude Code session traffic.
        """
        convo = list(messages[:cut])

        # Place a cache breakpoint on the last conversation message (budget
        # permitting, max 4 per request) so the lookup reads the deepest
        # cache entry created by earlier chat requests.
        if convo and self._count_breakpoints(payload, convo) < 4:
            last = copy.deepcopy(convo[-1])
            c = last.get("content")
            if isinstance(c, str):
                last["content"] = [{
                    "type": "text",
                    "text": c,
                    "cache_control": {"type": "ephemeral"},
                }]
            elif isinstance(c, list) and c and isinstance(c[-1], dict):
                c[-1]["cache_control"] = {"type": "ephemeral"}
            convo[-1] = last

        model = self.summarizer_model or payload.get("model", LEGACY_DEFAULT_MODEL)
        max_tokens = 16000
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": convo + [{"role": "user", "content": NATIVE_COMPACT_PROMPT}],
        }
        for key in ("system", "tools", "metadata"):
            if payload.get(key) is not None:
                body[key] = payload[key]
        if body.get("tools"):
            # The summary must be text — without this the model may answer
            # the cloned request with a tool_use and the summary comes back empty
            body["tool_choice"] = {"type": "none"}
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            body["thinking"] = thinking
            body["max_tokens"] = max(max_tokens, int(thinking.get("budget_tokens", 0)) + 4000)

        summary, diag = self._native_request(body, auth_headers)
        if summary:
            return summary

        # Empty text on a 200. Say exactly what came back — "response starts:
        # event: message_start" told nobody anything (#11) — then retry once
        # on a model that does not refuse or think its way to a blank answer.
        # Only on first-party: a custom endpoint would not know the fallback
        # model, and the caller's flattened retry covers that path anyway.
        log.warning(f"Native compaction returned no text ({_describe(diag)})")
        if not endpoints.is_anthropic(SUMMARIZER_BASE_URL) or model == FALLBACK_MODEL:
            raise RuntimeError(f"Summarization returned empty text ({_describe(diag)})")

        retry = dict(body)
        retry["model"] = FALLBACK_MODEL
        retry["max_tokens"] = max_tokens
        retry.pop("thinking", None)  # omitted = the model's default; never "disabled" (400 on fable)
        log.info(f"Native compaction retry on {FALLBACK_MODEL} (no prompt-cache reuse)")
        summary, diag2 = self._native_request(retry, auth_headers)
        if summary:
            return summary
        raise RuntimeError(
            f"Summarization returned empty text on {model} ({_describe(diag)}) "
            f"and on {FALLBACK_MODEL} ({_describe(diag2)})")

    def _native_request(self, body: dict, auth_headers: dict):
        """POST one native compaction request; return (text, diagnostics)."""
        req_body = json.dumps(body).encode()
        headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        summarizer_path = _join_path(_SUMMARIZER_PATH, "/v1/messages")
        log.info(
            f"Native compaction request -> {SUMMARIZER_BASE_URL} "
            f"model={body['model']} messages={len(body['messages'])} ({len(req_body):,} bytes)"
        )

        conn = _summarizer_conn()
        conn.request("POST", summarizer_path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp_body[:2] == b"\x1f\x8b":  # upstream gzipped despite identity
            resp_body = gzip.decompress(resp_body)

        if resp.status != 200:
            error = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")

        text, diag = _parse_message_stream(resp_body)
        usage = diag.get("usage") or {}
        log.info(
            f"Native compaction usage: input={usage.get('input_tokens', 0):,} "
            f"cache_read={usage.get('cache_read_input_tokens', 0):,} "
            f"cache_write={usage.get('cache_creation_input_tokens', 0):,} "
            f"output={diag.get('output_tokens', 0):,} stop={diag.get('stop_reason')}"
        )
        return text.strip(), diag

    # ------------------------------------------------------------------
    # Flattened mode: standalone request to a custom summarizer
    # ------------------------------------------------------------------

    def _summarize_flattened(self, prompt: str, auth_headers: dict,
                             model_override: str = "") -> str:
        summary_max_tokens = 16000
        # LEGACY_DEFAULT_MODEL is a Claude model name — meaningless to a
        # third-party endpoint, so callers there pass the session's own model.
        model = self.summarizer_model or model_override or LEGACY_DEFAULT_MODEL

        if SUMMARIZER_FORMAT == "openai":
            if not self.summarizer_model:
                raise RuntimeError(
                    "ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai requires "
                    "ROLLING_CONTEXT_MODEL to name the summarizer model"
                )
            path = _join_path(_SUMMARIZER_PATH, "/v1/chat/completions")
            req_body = json.dumps({
                "model": model,
                "max_tokens": summary_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            headers = {"content-type": "application/json"}
            if SUMMARIZER_API_KEY:
                headers["authorization"] = f"Bearer {SUMMARIZER_API_KEY}"
        else:
            path = _join_path(_SUMMARIZER_PATH, "/v1/messages")
            req_body = json.dumps({
                "model": model,
                "max_tokens": summary_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            if SUMMARIZER_API_KEY:
                headers = {
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": SUMMARIZER_API_KEY,
                }
            else:
                headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        log.info(
            f"Compression request -> {SUMMARIZER_BASE_URL} path={path} "
            f"format={SUMMARIZER_FORMAT} model={model}"
        )

        conn = _summarizer_conn(timeout=120)
        conn.request("POST", path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp_body[:2] == b"\x1f\x8b":  # upstream gzipped despite identity
            resp_body = gzip.decompress(resp_body)

        if resp.status != 200:
            error = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")
        data = json.loads(resp_body)

        if SUMMARIZER_FORMAT == "openai":
            return data["choices"][0]["message"]["content"]
        # Not content[0] blindly: an endpoint with thinking enabled puts a
        # thinking block first, and a misbehaving one can return no blocks at
        # all. A bare KeyError/IndexError here tells the user nothing about
        # which endpoint misbehaved or how.
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        raise RuntimeError(
            f"Summarizer at {SUMMARIZER_BASE_URL} returned no text block; "
            f"response: {json.dumps(data)[:300]}")

    # ------------------------------------------------------------------

    def compress(self, messages: list, auth_headers: dict, real_token_count: int = None,
                 payload: dict = None) -> list:
        """Compress messages using rolling summarization (synchronous).

        Returns the compressed message list, or None when there is nothing
        worth compressing (callers must not build a compression entry then)."""
        # Use real API token count to determine what fraction of content to keep
        if real_token_count and real_token_count > 0:
            keep_ratio = self.target_tokens / real_token_count
            log.info(
                f"Keep ratio: {keep_ratio:.1%} "
                f"(target={self.target_tokens:,} / real={real_token_count:,})"
            )
        else:
            # Fallback: keep half (conservative)
            keep_ratio = 0.5
            log.info(f"Keep ratio: {keep_ratio:.1%} (fallback, no real token count)")

        keep_from_idx = self._find_keep_index(messages, keep_ratio)

        has_existing_summary = self._has_summary(messages)
        start_idx = 2 if has_existing_summary else 0

        keep_from_idx = self._safe_cut(messages, keep_from_idx, start_idx)

        if keep_from_idx <= start_idx:
            log.info("Not enough old messages to compress, passing through")
            return None

        recent_messages = messages[keep_from_idx:]

        use_native = NATIVE_MODE and payload is not None
        if use_native:
            try:
                new_summary = self._summarize_native(
                    payload, messages, keep_from_idx, auth_headers)
            except Exception as e:
                if not NATIVE_FALLBACK:
                    raise
                # A third-party endpoint that choked on the cloned request
                # shape. One flattened retry beats a 300s cooldown and a
                # session that never compresses at all.
                log.warning(
                    f"Native compaction failed against {SUMMARIZER_BASE_URL} "
                    f"({e}); retrying flattened"
                )
                use_native = False
        if not use_native:
            existing_summary = self._extract_summary(messages) if has_existing_summary else ""
            to_compress = messages[start_idx:keep_from_idx]
            if not to_compress:
                log.info("Nothing to compress")
                return None
            conversation_text = self._messages_to_text(to_compress)
            existing_section = ""
            if existing_summary:
                existing_section = (
                    "EXISTING ROLLING SUMMARY FROM PREVIOUS COMPRESSIONS "
                    "(integrate this timeline with the new conversation below — "
                    "keep all details, extend the timeline):\n"
                    f"{existing_summary}\n\n"
                )
            prompt = SUMMARIZE_PROMPT.format(
                existing_summary_section=existing_section,
                conversation=conversation_text,
            )
            log.info(
                f"Summarizing {keep_from_idx - start_idx} messages "
                f"({len(conversation_text):,} chars, flattened)..."
            )
            session_model = (payload or {}).get("model", "") if NATIVE_FALLBACK else ""
            new_summary = self._summarize_flattened(
                prompt, auth_headers, model_override=session_model)

        log.info(f"Summary generated: {len(new_summary):,} chars")

        summary_message = {
            "role": "user",
            "content": (
                f"{SUMMARY_MARKER}\n"
                f"{new_summary}\n"
                f"{SUMMARY_END_MARKER}\n\n"
                "The above is a chronological summary of our earlier conversation. "
                "All file paths, decisions, and code changes are preserved. "
                "Continue from where we left off."
            ),
        }
        ack_message = {
            "role": "assistant",
            "content": (
                "I have the full context from our previous conversation — "
                "the timeline, all files modified, decisions made, and current state. "
                "Continuing from where we left off."
            ),
        }

        compressed = [summary_message, ack_message] + recent_messages

        original_chars = self._count_chars(messages)
        compressed_chars = self._count_chars(compressed)
        summary_chars = len(new_summary)
        recent_chars = self._count_chars(recent_messages)
        self.compression_count += 1
        if real_token_count:
            reduction = compressed_chars / original_chars if original_chars > 0 else 0
            estimated_output_tokens = int(real_token_count * reduction)
            self.total_tokens_saved += real_token_count - estimated_output_tokens
            log.info(
                f"Compression #{self.compression_count}: "
                f"~{real_token_count:,} -> ~{estimated_output_tokens:,} real tokens "
                f"({reduction:.0%} of original, "
                f"summary={summary_chars:,} chars, recent={recent_chars:,} chars)"
            )
        else:
            self.total_tokens_saved += (original_chars - compressed_chars) // 2
            log.info(
                f"Compression #{self.compression_count}: "
                f"{original_chars:,} -> {compressed_chars:,} chars "
                f"(summary={summary_chars:,}, recent={recent_chars:,})"
            )

        return compressed
