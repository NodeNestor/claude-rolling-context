# Rolling Context for Claude Code

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)

A transparent proxy that gives Claude Code **rolling context compression** — old messages get automatically summarized while recent messages stay fully verbatim. You never hit the context wall, and you never lose important details.

**Zero config.** Uses your existing Claude Code auth. No API key needed. Just install and forget.

> Claude Code's built-in `/compact` replaces your **entire** conversation with a lossy summary. After a few compactions, you're summarizing a summary of a summary. This plugin only compresses old messages — recent context stays untouched.

It's also a cost story: every token you carry in context gets re-billed on **every turn** (at cache-read rates), so an unmanaged session's input cost grows with the *square* of its length. Capping the prefix makes it linear — [the math](#the-economics-why-capping-the-prefix-matters) works in relative units and holds for every model, and it matters more the bigger the context window, not less.

## `/compact` vs Rolling Context

| | `/compact` (built-in) | Rolling Context |
|---|---|---|
| What gets compressed | Everything | Only old messages |
| Recent context | Summarized (lossy) | **Kept verbatim** |
| When it runs | Manual or near the context limit | Automatic, background |
| Latency impact | Blocks until done | Zero — async |
| After multiple compressions | Summary of summary of summary | Fresh rolling merge each time |
| Input cost over a long session | Grows with the square of session length | Grows linearly |
| Original transcript | Replaced | Preserved (JSONL unchanged) |

## The economics: why capping the prefix matters

No price cards needed — the argument works in **relative units** that hold for every Claude model. Take the model's fresh-input-token rate as `1×`. The API bills:

| Operation | Relative cost |
|---|---|
| Fresh input tokens | `1×` |
| Prompt-cache **read** | `0.1×` |
| Prompt-cache **write** | `1.25×` (5-min TTL) / `2×` (1-hour TTL) |
| Output tokens | `~5×` |

Claude Code re-sends the entire conversation on every turn. Even with caching working perfectly, each turn costs `prefix size × 0.1`. So a session's total input cost is **the sum of the prefix over all turns**:

- **Unmanaged context** (big window, auto-compact only near the limit): the prefix grows every turn, so total cost grows with the *square* of session length. A 1M window doesn't fix this — it just lets the prefix run to 900K+ before anything stops it, and every one of those tokens costs `0.1×` again on every single turn.
- **Rolling context**: the prefix is capped between `TARGET` and `TRIGGER`, so total cost grows *linearly*.

**The cache-miss blast radius matters even more in interactive use.** The prompt cache has a TTL. Read a diff, think for a while, get coffee — and the next turn re-*writes* the whole prefix at `1.25×`. At a 900K prefix, one cold turn bills the equivalent of ~1.1M fresh input tokens. With the prefix capped at ~100K, the identical cold turn is ~9× cheaper. Compression doesn't just shrink the average turn — it caps the worst one.

**What compression itself costs:** each cycle re-writes the new (much smaller) prefix once, and in native mode the summarization request is itself a cache read — a few hundred fresh tokens, measured (see below). Ballpark: sessions that accumulate past ~100K of context — a couple of hours of real work — come out ahead, and the gap compounds from there. Short sessions are a wash; don't expect magic on a 20-minute task.

**On Pro/Max subscriptions** none of this is dollars, but the same math applies in a different currency: rate-limit accounting weights cache reads far below fresh input, so the identical curves decide how fast you burn your 5-hour window.

> **Honest note:** if cost were the *only* goal, lowering Claude Code's auto-compact threshold (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) buys a similar spend curve for free. What it can't buy is quality under repetition: built-in compaction replaces the whole conversation with a lossy summary every time it fires — at a low threshold it fires often, and you're soon working from a summary of a summary of a summary. The rolling design exists so aggressive compression doesn't cost you the session: recent work stays verbatim, and old work lives in one continuously-merged timeline instead of N generations of loss.

## How It Works

```
Claude Code  ──►  Rolling Context Proxy (:5588)  ──►  Anthropic API
                         │
                         ├─ context < 100K tokens? pass through unchanged
                         │
                         └─ context > 100K tokens?
                              1. summarize old messages in the background
                                 (native mode: your session's own model,
                                  served almost entirely from prompt cache)
                              2. keep ~40K tokens of recent messages verbatim
                              3. inject compressed context on next request
                              4. never blocks, never adds latency
```

Instead of replacing everything, this plugin:

1. **Keeps recent messages untouched** — recent context stays verbatim
2. **Only compresses when needed** — triggers at 100K (real API token count), compresses old messages, grows naturally until next trigger
3. **Merges summaries** — each compression cycle merges with the previous summary, building a rolling timeline
4. **Never blocks** — compression runs in the background, applied on the next request
5. **Full transcripts preserved** — Claude Code still saves everything to JSONL in `~/.claude/projects/`

## Install

### Option 1: Claude Code Plugin (recommended)

Run these two commands inside Claude Code:

```
/plugin marketplace add https://github.com/NodeNestor/nestor-plugins
/plugin install rolling-context
```

Restart your terminal and start a new Claude Code session. On the **first start**, the plugin configures `ANTHROPIC_BASE_URL` and starts the proxy. Since the env var only takes effect on the next terminal, **restart your terminal once more** — after that, everything works automatically. No pip install needed — pure Python stdlib.

### Option 2: Manual install

**Linux / macOS:**
```bash
git clone https://github.com/NodeNestor/claude-rolling-context.git ~/claude-rolling-context
cd ~/claude-rolling-context
bash install.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/NodeNestor/claude-rolling-context.git $HOME\claude-rolling-context
cd $HOME\claude-rolling-context
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer configures `ANTHROPIC_BASE_URL` and registers the plugin. Restart your terminal and you're done. Requires Python 3.7+ (no pip install needed — pure stdlib).

## How Compression Works

When the message array exceeds the trigger threshold:

```
BEFORE (hit 100K trigger):
  [msg1] [msg2] [msg3] ... [msg60] [msg61] ... [msg100]
  |<——————————————— ~105K tokens ——————————————>|

AFTER (compressed):
  [rolling summary] [ack] [msg61] ... [msg100]
  |<— ~5K summary —>|    |<—— verbatim ————————>|

NEXT CYCLE (grows back to 100K, triggers again):
  [rolling summary] [ack] [msg61] ... [msg140]
  |<——————————————— ~105K tokens ——————————————>|
  → new summary merges old summary + msg61-msg100
  → keeps msg101-msg140 verbatim
```

The summary preserves a structured record of everything that happened:

- **Active Goal** — what the user is currently asking for, constraints, do/don't rules
- **Previous Goals** — completed or shifted-away-from goals (kept brief)
- **Timeline** — chronological numbered steps: every file change, decision, error, and user instruction
- **Current State** — what's done, in progress, and next
- **Key Details** — file paths, configs, decisions that must survive compression

Goals evolve naturally across rolling compressions — the latest request stays prominent while completed goals move to the previous section. User instructions are never lost.

### How the summary is generated (native mode)

By default the proxy doesn't build a separate summarization request. It **clones the exact request Claude Code just sent** — same model, system prompt, and tools, with the conversation truncated at the cut point — and appends one user message asking for the summary (the same way Claude Code's own `/compact` works). Two big wins:

- **It's a prompt-cache read.** The cloned prefix was just sent by the chat request, so the API serves it from cache. Measured in practice: a ~72K-token compression request cost ~400 fresh input tokens.
- **It's genuine Claude Code session traffic.** Pro/Max subscription OAuth tokens are classified server-side — standalone requests that don't look like Claude Code get routed to the overage lane and rejected with 429. The cloned request passes because it *is* the session's own request shape.

Setting `ROLLING_CONTEXT_MODEL` pins a different summarizer model (the request shape stays native, but a different model means no prompt-cache reuse). Configuring any `ROLLING_CONTEXT_SUMMARIZER_*` variable switches to a standalone flattened request instead — see below.

### Using any API or a local model for compression

Summarization can run on a completely separate endpoint — any Anthropic-format API, or any OpenAI-compatible one (Ollama, LM Studio, vLLM, OpenRouter, DeepSeek, Groq, ...):

```bash
# Separate Anthropic API key (billed there instead of your subscription)
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-ant-api03-...

# Local model via Ollama / LM Studio / vLLM (OpenAI-compatible)
export ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:11434
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_MODEL=qwen3:8b   # required for openai format

# OpenRouter (or any hosted OpenAI-compatible API)
export ROLLING_CONTEXT_SUMMARIZER_URL=https://openrouter.ai/api
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-or-...
export ROLLING_CONTEXT_MODEL=deepseek/deepseek-chat
```

## Architecture

The proxy is **fully stateless** — no sessions, no databases, no tracking. It works by hashing message content:

1. When a response comes back from the API with a high token count, the proxy compresses the messages and stores the result keyed by content hashes
2. On the next request, it hashes the incoming messages and checks for a matching compression
3. If found, it swaps in the compressed version transparently

This means:
- **Multiple conversations work automatically** — each conversation has unique content, unique hashes, no collision
- **Subagents and branches just work** — the proxy doesn't care about sessions, only content
- **No state to corrupt** — restart the proxy anytime, worst case is one extra compression cycle
- **Claude Code sees nothing different** — the proxy is invisible, JSONL transcripts are unmodified

## Configuration

All settings via environment variables (all optional — defaults work great):

| Variable | Default | Description |
|----------|---------|-------------|
| `ROLLING_CONTEXT_TRIGGER` | `100000` | Compress when context exceeds this many tokens |
| `ROLLING_CONTEXT_TARGET` | `40000` | Keep this many tokens of recent messages after compression |
| `ROLLING_CONTEXT_MODEL` | *(session model)* | Summarizer model; unset = the session's own model (prompt-cache hit) |
| `ROLLING_CONTEXT_PORT` | `5588` | Proxy listen port |
| `ROLLING_CONTEXT_UPSTREAM` | `https://api.anthropic.com` | Upstream API URL (chain to another proxy!) |
| `ROLLING_CONTEXT_SUMMARIZER_URL` | *(upstream)* | Custom endpoint for summarization (local model, other API) |
| `ROLLING_CONTEXT_SUMMARIZER_KEY` | *(uses Claude Code auth)* | API key for custom summarizer endpoint |
| `ROLLING_CONTEXT_SUMMARIZER_FORMAT` | `anthropic` | `openai` = /v1/chat/completions for OpenAI-compatible endpoints |
| `ROLLING_CONTEXT_FAILURE_COOLDOWN` | `300` | Seconds to wait before retrying after a failed compression |

## Proxy Chaining

Already using another proxy (model router, API gateway, etc.)? Rolling Context auto-detects this and chains through it:

```
Claude Code  ──►  Rolling Context (:5588)  ──►  Your Proxy  ──►  Anthropic API
```

If `ANTHROPIC_BASE_URL` is already set when you install, the plugin automatically saves it as `ROLLING_CONTEXT_UPSTREAM` and inserts itself in front. No manual config needed.

You can also set it explicitly:
```bash
export ROLLING_CONTEXT_UPSTREAM=http://localhost:8080  # your existing proxy
```

## Health Check

```bash
curl http://127.0.0.1:5588/health
```

Returns compression stats: how many compressions, tokens saved, etc.

## Debug

```bash
curl http://127.0.0.1:5588/debug/compressions
```

Returns the stored compression entries with their full summary content — useful for verifying what the rolling summary captured and whether user goals/instructions survived compression.

## Uninstall

Run the uninstall script — it handles both manual and marketplace installs, stops the proxy, cleans env vars, and removes all plugin registrations.

**Linux / macOS:**
```bash
cd ~/claude-rolling-context && bash uninstall.sh
```

**Windows (PowerShell):**
```powershell
cd $HOME\claude-rolling-context; powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

If you installed via marketplace and already deleted the repo, you can run it from the cache:
```powershell
cd $HOME\.claude\plugins\cache\rolling-context-marketplace\rolling-context\<version>
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

## License

MIT
