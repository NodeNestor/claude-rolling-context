# Rolling Context for Claude Code

A transparent proxy plugin that gives Claude Code **rolling context compression** — old messages get summarized while recent messages stay verbatim. You never hit the context wall, and you never lose important information.

## How It Works

```
Claude Code → [Rolling Context Proxy :5588] → Anthropic API
                      ↓
          if tokens > 80K (trigger):
            summarize old messages (Haiku)
            keep ~40K tokens of recent messages verbatim
            → context drops from 80K+ back to ~45K
            → grows naturally until next trigger
```

Instead of Claude Code's built-in compaction (which replaces EVERYTHING with a lossy summary), this plugin:

1. **Keeps recent messages untouched** — ~40K tokens of recent context stays verbatim
2. **Only compresses when needed** — triggers at 80K, compresses down to ~40K, then grows naturally until next trigger
3. **Merges summaries** — each compression cycle merges with the previous summary, building a rolling context
4. **Claude Code still saves full transcripts** — nothing is ever lost (JSONL in `~/.claude/projects/`)

## Install

```bash
git clone <repo-url> ~/claude-rolling-context
cd ~/claude-rolling-context
bash install.sh
```

The installer:
- Sets up a Python venv with dependencies
- Adds `ANTHROPIC_BASE_URL` to your shell profile (points Claude Code at the proxy)
- Registers as a Claude Code plugin (auto-starts proxy on session start)

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ROLLING_CONTEXT_PORT` | `5588` | Proxy listen port |
| `ROLLING_CONTEXT_TRIGGER` | `80000` | Compress when context exceeds this many tokens |
| `ROLLING_CONTEXT_TARGET` | `40000` | Keep this many tokens of recent messages after compression |
| `ROLLING_CONTEXT_MODEL` | `claude-haiku-latest` | Model used for summarization |
| `ROLLING_CONTEXT_UPSTREAM` | `https://api.anthropic.com` | Upstream API URL (chain to another proxy!) |
| `ROLLING_CONTEXT_SUMMARIZER_URL` | `https://api.anthropic.com` | Where Haiku summarization calls go |
| `ANTHROPIC_API_KEY` | *(none)* | API key for summarization calls. **Required** if Claude Code uses OAuth (which it does by default). Haiku summarization is very cheap (~$0.001/compression). |

## How It Compresses

When the message array exceeds the trigger threshold:

```
BEFORE (hit 80K trigger):
  [msg1] [msg2] [msg3] ... [msg60] [msg61] ... [msg100]
  ←————————————— ~85K tokens ——————————————→

AFTER (compressed to ~45K):
  [rolling summary] [ack] [msg61] ... [msg100]
  ← ~5K summary →         ←—— ~40K verbatim ——→

LATER (grows back to 80K, triggers again):
  [rolling summary] [ack] [msg61] ... [msg100] ... [msg140]
  ←————————————— ~82K tokens ——————————————→
  compresses again → merges old summary + msg61-msg120 into new summary
```

The summary is always dense and technical — preserving file paths, code changes, decisions, and architecture.

## Uninstall

```bash
bash uninstall.sh
```

## Health Check

```bash
curl http://127.0.0.1:5588/health
```

Returns compression stats (how many times compressed, tokens saved, etc).
