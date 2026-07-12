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

log = logging.getLogger("rolling-context.compressor")

_default_summarizer_url = os.environ.get("ROLLING_CONTEXT_UPSTREAM") or "https://api.anthropic.com"
SUMMARIZER_URL_SET = bool(os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL"))
SUMMARIZER_BASE_URL = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL") or _default_summarizer_url
SUMMARIZER_API_KEY = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY") or ""
# "anthropic" (default) or "openai" — openai speaks /v1/chat/completions
SUMMARIZER_FORMAT = (os.environ.get("ROLLING_CONTEXT_SUMMARIZER_FORMAT") or "anthropic").lower()
# Any custom summarizer config switches off native mode
NATIVE_MODE = not (SUMMARIZER_URL_SET or SUMMARIZER_API_KEY or SUMMARIZER_FORMAT != "anthropic")
LEGACY_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

ssl_ctx = ssl.create_default_context()

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
# like Claude Code's own /compact. Contains "context compressor" so test mocks
# can recognize summarization requests.
NATIVE_COMPACT_PROMPT = f"""Pause the current task. Act as a context compressor: produce a CHRONOLOGICAL, DENSE technical summary of the conversation above.

IMPORTANT: this compression request is NOT part of the conversation. Do not mention it in the summary, do not add it to the timeline, and do not treat it as the user's request. The Active Goal is the user's most recent REAL request from the conversation above — the task in progress continues after compression exactly where it left off, so summarize it as in-progress work, not as interrupted.

{SUMMARY_RULES}

If the conversation begins with a {SUMMARY_MARKER} block from an earlier compression, integrate it — keep all its details and extend the timeline with what happened since.

Write ONLY the chronological summary, nothing else."""

# Flattened mode: standalone prompt carrying the conversation as text.
SUMMARIZE_PROMPT = f"""You are a context compressor for an AI coding assistant conversation.

Your job: take the conversation below and produce a CHRONOLOGICAL, DENSE technical summary.

{SUMMARY_RULES}

{{existing_summary_section}}

CONVERSATION TO COMPRESS:
{{conversation}}

Write the chronological summary:"""


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

        req_body = json.dumps(body).encode()
        headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        summarizer_path = _join_path(_SUMMARIZER_PATH, "/v1/messages")
        log.info(
            f"Native compaction request -> {SUMMARIZER_BASE_URL} "
            f"model={model} messages={len(body['messages'])} ({len(req_body):,} bytes)"
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

        parts = []
        for line in resp_body.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            evt = data.get("type", "")
            if evt == "message_start":
                usage = data.get("message", {}).get("usage", {})
                log.info(
                    f"Native compaction usage: input={usage.get('input_tokens', 0):,} "
                    f"cache_read={usage.get('cache_read_input_tokens', 0):,} "
                    f"cache_write={usage.get('cache_creation_input_tokens', 0):,}"
                )
            elif evt == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif evt == "error":
                raise RuntimeError(f"Summarization stream error: {json.dumps(data)[:500]}")
        summary = "".join(parts).strip()
        if not summary:
            snippet = resp_body.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Summarization returned empty text; response starts: {snippet}")
        return summary

    # ------------------------------------------------------------------
    # Flattened mode: standalone request to a custom summarizer
    # ------------------------------------------------------------------

    def _summarize_flattened(self, prompt: str, auth_headers: dict) -> str:
        summary_max_tokens = 16000
        model = self.summarizer_model or LEGACY_DEFAULT_MODEL

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
        return data["content"][0]["text"]

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
            new_summary = self._summarize_native(payload, messages, keep_from_idx, auth_headers)
        else:
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
            new_summary = self._summarize_flattened(prompt, auth_headers)

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
