#!/usr/bin/env python3
"""Regression suite for issue #11 — a 200 with no text must not cost 300s.

On claude-fable-5 the native compaction request can return HTTP 200 with
stop_reason "refusal" and no content blocks, or a thinking block and no text.
The old parser only collected text deltas, reported "empty text; response
starts: event: message_start", and the conversation cooled down for 300s —
then failed identically on the next attempt, forever.

Now: the failure line names stop_reason / stop_details / block types, and the
compaction is retried once on a non-reasoning fallback model. Runnable
directly, no framework:

    python tests/test_empty_summary.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import compressor  # noqa: E402

_fails = 0


def check(label, cond, extra=""):
    global _fails
    if cond:
        print(f"  ok  {label}")
    else:
        _fails += 1
        print(f"  FAIL {label} {extra}")


def sse(events):
    return "".join("event: {}\ndata: {}\n\n".format(e["type"], json.dumps(e))
                   for e in events).encode()


def start(model):
    return {"type": "message_start", "message": {
        "id": "msg", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None,
        "usage": {"input_tokens": 708, "output_tokens": 1,
                  "cache_creation_input_tokens": 105340, "cache_read_input_tokens": 0}}}


def text_stream(model, text):
    return sse([
        start(model),
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ])


def refusal_stream(model):
    # What a classifier refusal looks like on the wire: no content blocks at all.
    return sse([
        start(model),
        {"type": "message_delta",
         "delta": {"stop_reason": "refusal", "stop_sequence": None,
                   "stop_details": {"type": "refusal", "category": "reasoning_extraction",
                                    "explanation": "declined"}},
         "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ])


def thinking_only_stream(model):
    return sse([
        start(model),
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "let me think " * 50}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
         "usage": {"output_tokens": 16000}},
        {"type": "message_stop"},
    ])


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConn:
    """Stands in for http.client; answers from a queue and records requests."""
    sent = []
    queue = []

    def __init__(self, *a, **k):
        pass

    def request(self, method, path, body=None, headers=None):
        FakeConn.sent.append(json.loads(body))

    def getresponse(self):
        return FakeResponse(FakeConn.queue.pop(0))

    def close(self):
        pass


def run(payload, *responses, base_url="https://api.anthropic.com"):
    FakeConn.sent = []
    FakeConn.queue = list(responses)
    compressor._summarizer_conn = lambda timeout=600: FakeConn()
    compressor.SUMMARIZER_BASE_URL = base_url
    rc = compressor.RollingCompressor()
    messages = payload["messages"]
    try:
        return rc._summarize_native(payload, messages, len(messages) - 1, {}), None
    except RuntimeError as e:
        return None, str(e)


PAYLOAD = {
    "model": "claude-fable-5",
    "max_tokens": 32000,
    "stream": True,
    "thinking": {"type": "adaptive"},
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}],
    "messages": [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": [{"type": "text", "text": "did it"}]},
        {"role": "user", "content": "next"},
    ],
}


def main():
    print("issue #11: empty summary on a 200")

    print(" text stream works as before")
    text, err = run(PAYLOAD, text_stream("claude-fable-5", "SUMMARY one"))
    check("summary returned", text == "SUMMARY one", err or "")
    check("single request", len(FakeConn.sent) == 1)
    check("session model used first", FakeConn.sent[0]["model"] == "claude-fable-5")
    check("adaptive thinking is not forwarded as budget", "thinking" not in FakeConn.sent[0])

    print(" refusal on fable -> retried on the fallback model")
    text, err = run(PAYLOAD, refusal_stream("claude-fable-5"),
                    text_stream(compressor.FALLBACK_MODEL, "SUMMARY two"))
    check("summary returned from the retry", text == "SUMMARY two", err or "")
    check("two requests", len(FakeConn.sent) == 2)
    check("retry uses the fallback model",
          FakeConn.sent[1]["model"] == compressor.FALLBACK_MODEL, FakeConn.sent[1]["model"])
    check("retry carries no thinking param", "thinking" not in FakeConn.sent[1])
    check("retry keeps tool_choice none", FakeConn.sent[1].get("tool_choice") == {"type": "none"})
    check("retry keeps the same conversation",
          FakeConn.sent[1]["messages"] == FakeConn.sent[0]["messages"])

    print(" thinking-only stream -> retried too")
    text, err = run(PAYLOAD, thinking_only_stream("claude-fable-5"),
                    text_stream(compressor.FALLBACK_MODEL, "SUMMARY three"))
    check("summary returned from the retry", text == "SUMMARY three", err or "")

    print(" both empty -> the error says why")
    text, err = run(PAYLOAD, refusal_stream("claude-fable-5"),
                    refusal_stream(compressor.FALLBACK_MODEL))
    check("raises", text is None and err)
    check("names stop_reason", "stop_reason=refusal" in (err or ""), err)
    check("names the category", "reasoning_extraction" in (err or ""), err)
    check("names both models",
          "claude-fable-5" in (err or "") and compressor.FALLBACK_MODEL in (err or ""), err)
    check("never the old useless snippet", "response starts" not in (err or ""))

    print(" thinking-only + max_tokens is described as such")
    text, err = run(PAYLOAD, thinking_only_stream("claude-fable-5"),
                    thinking_only_stream(compressor.FALLBACK_MODEL))
    check("stop_reason=max_tokens", "stop_reason=max_tokens" in (err or ""), err)
    check("thinking chars counted", "thinking_chars=" in (err or ""), err)
    check("block types listed", "blocks=['thinking']" in (err or ""), err)

    print(" custom endpoint: no model fallback, one request, clear error")
    text, err = run(PAYLOAD, refusal_stream("glm-4.6"), base_url="https://api.z.ai/api/anthropic")
    check("single request", len(FakeConn.sent) == 1)
    check("raises with diagnostics", err and "stop_reason=refusal" in err, err)

    print(" already on the fallback model: no second request")
    p = dict(PAYLOAD, model=compressor.FALLBACK_MODEL)
    text, err = run(p, refusal_stream(compressor.FALLBACK_MODEL))
    check("single request", len(FakeConn.sent) == 1)
    check("raises", text is None and err)

    print(" endpoint that ignored stream=true")
    body = json.dumps({"id": "m", "type": "message", "role": "assistant", "model": "x",
                       "content": [{"type": "thinking", "thinking": "hm"},
                                   {"type": "text", "text": "SUMMARY json"}],
                       "stop_reason": "end_turn", "usage": {"output_tokens": 3}}).encode()
    text, err = run(PAYLOAD, body)
    check("text extracted from a JSON message", text == "SUMMARY json", err or "")

    print(" garbage body")
    text, err = run(PAYLOAD, b"<html>nope</html>", b"<html>nope</html>")
    check("raises with the body head", err and "no SSE events" in err and "nope" in err, err)

    if _fails:
        print(f"\n{_fails} FAILED")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
