"""
Rolling Context Compressor

When context exceeds trigger_tokens, compresses old messages down to target_tokens
of recent context + a dense chronological summary of everything before.

Pure stdlib — no external dependencies.
"""

import json
import os
import ssl
import logging
from urllib.request import Request, urlopen

log = logging.getLogger("rolling-context.compressor")

SUMMARIZER_BASE_URL = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL", "https://api.anthropic.com")
SUMMARY_RATIO = float(os.environ.get("ROLLING_CONTEXT_SUMMARY_RATIO", "0.25"))

ssl_ctx = ssl.create_default_context()

SUMMARY_MARKER = "[ROLLING_CONTEXT_SUMMARY]"
SUMMARY_END_MARKER = "[/ROLLING_CONTEXT_SUMMARY]"

SUMMARIZE_PROMPT = """You are a context compressor for an AI coding assistant conversation.

Your job: take the conversation below and produce a CHRONOLOGICAL, DENSE technical summary.

RULES:
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
- [File paths, configs, decisions that must not be forgotten]

{existing_summary_section}

CONVERSATION TO COMPRESS:
{conversation}

Write the chronological summary:"""


class RollingCompressor:
    def __init__(
        self,
        trigger_tokens: int = 80000,
        target_tokens: int = 40000,
        summarizer_model: str = "claude-haiku-latest",
    ):
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
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

    def estimate_tokens(self, messages: list) -> int:
        """Rough token estimate for logging. Not used for compression decisions."""
        return self._count_chars(messages) // 4

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

    def compress(self, messages: list, auth_headers: dict, real_token_count: int = None) -> list:
        """Compress messages using rolling summarization (synchronous)."""
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

        if keep_from_idx <= start_idx:
            log.info("Not enough old messages to compress, passing through")
            return messages

        existing_summary = self._extract_summary(messages) if has_existing_summary else ""
        to_compress = messages[start_idx:keep_from_idx]
        recent_messages = messages[keep_from_idx:]

        if not to_compress:
            log.info("Nothing to compress")
            return messages

        conversation_text = self._messages_to_text(to_compress)

        input_tokens = self.estimate_tokens(to_compress)
        if existing_summary:
            input_tokens += len(existing_summary) // 4
        summary_max_tokens = max(2000, min(16000, int(input_tokens * SUMMARY_RATIO)))

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
            f"Summarizing {len(to_compress)} messages (~{input_tokens:,} tokens) "
            f"with {self.summarizer_model} (max_tokens={summary_max_tokens:,})..."
        )

        req_body = json.dumps({
            "model": self.summarizer_model,
            "max_tokens": summary_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        headers = dict(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        req = Request(
            f"{SUMMARIZER_BASE_URL}/v1/messages?beta=true",
            data=req_body,
            headers=headers,
            method="POST",
        )
        with urlopen(req, context=ssl_ctx, timeout=120) as resp:
            if resp.status != 200:
                error = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")
            data = json.loads(resp.read())

        new_summary = data["content"][0]["text"]
        summary_tokens = len(new_summary) // 4
        log.info(f"Summary generated: ~{summary_tokens:,} tokens ({len(new_summary)} chars)")

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

        original_tokens = self.estimate_tokens(messages)
        compressed_tokens = self.estimate_tokens(compressed)
        self.compression_count += 1
        self.total_tokens_saved += original_tokens - compressed_tokens

        log.info(
            f"Compression #{self.compression_count}: "
            f"{original_tokens:,} -> {compressed_tokens:,} tokens "
            f"(saved {original_tokens - compressed_tokens:,}, "
            f"summary={summary_tokens:,}, recent={self.estimate_tokens(recent_messages):,})"
        )

        return compressed
