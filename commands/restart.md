---
description: Restart the rolling-context proxy now (e.g. to pick up a plugin update the start hook deferred because other sessions were mid-request)
---

Run this command and show the user the log lines it prints, verbatim. Add no
commentary beyond one short line if something looks wrong.

The start hook never downgrades a running proxy and never replaces one that
has requests in flight from other sessions — it defers such upgrades. This
forces the restart: it still waits up to ~6 seconds for in-flight requests to
finish, then replaces whatever is serving with this plugin's version.

```
export ROLLING_CONTEXT_FORCE_RESTART=1; (powershell -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/hooks/start-proxy.ps1" 2>/dev/null || bash "${CLAUDE_PLUGIN_ROOT}/hooks/start-proxy.sh" 2>/dev/null); tail -n 6 "$HOME/.claude/rolling-context-hook.log"
```
