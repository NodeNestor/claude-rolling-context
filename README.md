# Rolling Context for Claude Code

A transparent proxy that gives Claude Code **rolling context compression** — old messages get automatically summarized while recent messages stay fully verbatim. You never hit the context wall, and you never lose important details.

**Zero config.** Uses your existing Claude Code auth. No API key needed. Just install and forget.

## How It Works

```
Claude Code  ──►  Rolling Context Proxy (:5588)  ──►  Anthropic API
                         │
                         ├─ if API reports > trigger (80K default):
                         │    1. summarize old messages with Haiku (background, async)
                         │    2. keep ~40K tokens of recent messages verbatim
                         │    3. inject compressed context on next request
                         │
                         └─ never blocks, never adds latency
```

Instead of Claude Code's built-in `/compact` (which replaces **everything** with a lossy summary), this plugin:

1. **Keeps recent messages untouched** — ~40K tokens of recent context stays verbatim
2. **Only compresses when needed** — triggers at 80K, compresses down to ~45K, grows naturally until next trigger
3. **Merges summaries** — each compression cycle merges with the previous summary, building a rolling timeline
4. **Never blocks** — compression runs in the background, applied on the next request
5. **Full transcripts preserved** — Claude Code still saves everything to JSONL in `~/.claude/projects/`

## Install

### Option 1: Claude Code Plugin (recommended)

Run these two commands inside Claude Code:

```
/plugin marketplace add https://github.com/NodeNestor/claude-rolling-context
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

## Configuration

All settings via environment variables (all optional — defaults work great):

| Variable | Default | Description |
|----------|---------|-------------|
| `ROLLING_CONTEXT_TRIGGER` | `80000` | Compress when context exceeds this many tokens |
| `ROLLING_CONTEXT_TARGET` | `40000` | Keep this many tokens of recent messages after compression |
| `ROLLING_CONTEXT_MODEL` | `claude-haiku-4-5-20251001` | Model used for summarization |
| `ROLLING_CONTEXT_PORT` | `5588` | Proxy listen port |
| `ROLLING_CONTEXT_UPSTREAM` | `https://api.anthropic.com` | Upstream API URL (chain to another proxy!) |

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

## How Compression Works

When the message array exceeds the trigger threshold:

```
BEFORE (hit 80K trigger):
  [msg1] [msg2] [msg3] ... [msg60] [msg61] ... [msg100]
  |<——————————————— ~85K tokens ———————————————>|

AFTER (compressed to ~45K):
  [rolling summary] [ack] [msg61] ... [msg100]
  |<— ~5K summary —>|    |<—— ~40K verbatim ——>|

NEXT CYCLE (grows back to 80K, triggers again):
  [rolling summary] [ack] [msg61] ... [msg140]
  |<——————————————— ~82K tokens ———————————————>|
  → merges old summary + msg61-msg100 into new summary
  → keeps msg101-msg140 verbatim
```

The summary preserves a chronological timeline of everything that happened — file paths, code changes, decisions, errors, and current state. Dense and technical, not lossy.

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
cd $HOME\.claude\plugins\cache\rolling-context-marketplace\rolling-context\1.0.0
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

## Health Check

```bash
curl http://127.0.0.1:5588/health
```

Returns compression stats: how many compressions, tokens saved, etc.

## How It's Different from `/compact`

| | `/compact` (built-in) | Rolling Context |
|---|---|---|
| What gets compressed | Everything | Only old messages |
| Recent context | Summarized | Kept verbatim |
| When it runs | Manual or at threshold | Automatic, background |
| Latency impact | Blocks until done | Zero — async |
| Summary quality | Single pass | Rolling merge, preserves timeline |
| Original transcript | Replaced | Preserved (JSONL unchanged) |

## License

MIT
