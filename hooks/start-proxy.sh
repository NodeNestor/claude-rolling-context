#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Pure stdlib — no venv needed, just python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
VERFILE="$HOME/.claude/rolling-context-proxy.version"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"
CURRENT_VERSION=$(cat "$SCRIPT_DIR/../.claude-plugin/plugin.json" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG"
}

# Detect Windows (git bash)
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]]; then
    IS_WINDOWS=true
else
    IS_WINDOWS=false
fi

_python() {
    if [ "$IS_WINDOWS" = true ]; then
        echo "python"
    elif command -v python3 &>/dev/null; then
        echo "python3"
    else
        echo "python"
    fi
}
PYTHON_CMD=$(_python)

log "Hook started. PROXY_DIR=$PROXY_DIR IS_WINDOWS=$IS_WINDOWS"

# Always update settings.json first (even if proxy is already running)
SETTINGS_FILE="$HOME/.claude/settings.json"
update_settings() {
    $PYTHON_CMD - "$SETTINGS_FILE" "$PROXY_URL" <<'PYEOF'
import json, sys, os
from urllib.parse import urlparse

settings_file = sys.argv[1]
proxy_url = sys.argv[2]

settings = {}
if os.path.exists(settings_file):
    try:
        # utf-8-sig, not the locale default: a UTF-8 BOM must not read as a
        # corrupt file. On Windows the PowerShell hook wrote one, and git-bash
        # shares the same $HOME, so this script saw a BOM'd file, called it
        # unparseable, and rewrote it from {} below.
        with open(settings_file, "r", encoding="utf-8-sig") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, IOError, OSError, UnicodeDecodeError):
        # Refuse to write. The file exists but we cannot read it, and
        # regenerating it from {} would destroy the user's entire global
        # config — permissions, hooks, enabledPlugins, theme, all of it.
        # Losing the proxy chaining is recoverable; losing their settings
        # is not.
        print("unreadable")
        sys.exit(0)

if not isinstance(settings, dict):
    print("unreadable")
    sys.exit(0)

if "env" not in settings or not isinstance(settings["env"], dict):
    settings["env"] = {}

env = settings["env"]

# Set ANTHROPIC_BASE_URL
def points_at_us(url):
    """True only if url is OUR proxy — loopback AND our port.

    Testing for the bare string "127.0.0.1" treated every LOCAL MODEL endpoint
    as if it were the proxy already installed: Ollama on 11434, llama.cpp,
    LM Studio on 1234, vLLM on 8000. Chaining was then skipped, so the plugin
    sat there doing nothing for exactly the users the README tells to run local
    models. The port is what distinguishes us; the host alone does not.
    """
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        ours = urlparse(proxy_url)
        return (u.hostname in ("127.0.0.1", "localhost", "::1")
                and (u.port or 80) == (ours.port or 80))
    except Exception:
        return False


existing = env.get("ANTHROPIC_BASE_URL", "")
if not existing:
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print("set")
elif not points_at_us(existing):
    env["ROLLING_CONTEXT_UPSTREAM"] = existing
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print("chained")
else:
    print("already")

# Set plugin config defaults (only if not already present)
defaults = {
    "ROLLING_CONTEXT_PORT": "5588",
    "ROLLING_CONTEXT_TRIGGER": "100000",
    "ROLLING_CONTEXT_TARGET": "40000",
}
for key, value in defaults.items():
    if key not in env:
        env[key] = value

# Unset ROLLING_CONTEXT_MODEL = compress with the session's own model
# (prompt-cache hit). Migrate away the old seeded haiku default.
if env.get("ROLLING_CONTEXT_MODEL") == "claude-haiku-4-5-20251001":
    del env["ROLLING_CONTEXT_MODEL"]

with open(settings_file, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF
}

RESULT=$(update_settings 2>/dev/null)
case "$RESULT" in
    set)     log "Set ANTHROPIC_BASE_URL=$PROXY_URL (settings.json)" ;;
    chained) log "Chaining upstream (settings.json)" ;;
    already) log "ANTHROPIC_BASE_URL already set (settings.json)" ;;
    unreadable)
        log "WARNING: settings.json exists but could not be parsed — left untouched." ;;
    *)       log "WARNING: Could not update settings.json" ;;
esac

# Check if proxy is already running
_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
}

_pid_alive() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

_is_our_proxy() {
    # Identity, not just liveness. `kill -0` answers "is SOME process wearing
    # this number", and after a crash the kernel hands the dead proxy's number
    # to whatever starts next. Killing on liveness alone would kill a stranger.
    local pid="$1"
    [ -n "$pid" ] || return 1
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -NoProfile -Command "\$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$pid\" -ErrorAction SilentlyContinue; if (\$p -and \$p.CommandLine -like '*server.py*') { exit 0 } else { exit 1 }" 2>/dev/null
    elif [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "server\.py"
    else
        ps -p "$pid" -o args= 2>/dev/null | grep -q "server\.py"
    fi
}

_stop_pid() {
    local pid="$1" source="$2"
    [ -n "$pid" ] || return 0
    _pid_alive "$pid" || return 0
    if _is_our_proxy "$pid"; then
        log "Stopping proxy PID $pid ($source)"
        _kill_pid "$pid"
    else
        log "PID $pid ($source) is alive but is not our proxy — recycled PID, leaving that process alone"
    fi
}

_clear_recorded_proxy() {
    # $1 = true when something is still serving on the port. The PID reported
    # by /health outranks the PID file: it comes from the process that is
    # actually holding the port, while the file is a copy that a crash, a lost
    # bind race or a manual start can leave pointing anywhere.
    if [ "$1" = "serving" ]; then
        _stop_pid "$(_probe_pid)" "reported by /health"
    fi
    _stop_pid "$(cat "$PIDFILE" 2>/dev/null | tr -d '[:space:]')" "from the PID file"
    rm -f "$PIDFILE" "$VERFILE"
}

_probe() {
    $PYTHON_CMD "$SCRIPT_DIR/probe.py" "$PORT" "${1:-2}" 2>/dev/null
}

_probe_pid() {
    $PYTHON_CMD "$SCRIPT_DIR/probe.py" "$PORT" "${1:-2}" pid 2>/dev/null | tr -d '[:space:]'
}

# Ask the PORT, not the PID file. The PID file only records an intention to
# run; the port is where "running" is either true or it is not. See probe.py
# and issue #9 for the failure this replaces.
PROBE=$(_probe 2)
case "$PROBE" in
    "ours $CURRENT_VERSION")
        log "Proxy already running and healthy on :$PORT (v$CURRENT_VERSION)"
        exit 0
        ;;
    ours*)
        log "Version changed (${PROBE#ours } -> $CURRENT_VERSION), restarting proxy"
        _clear_recorded_proxy serving
        # The old proxy owns the port until it actually exits. Starting on top
        # of it would just lose the bind and leave the old version serving
        # while the log claimed a restart.
        # Bounded by the clock, not by a probe count: each probe costs an
        # interpreter start, and the SessionStart hook has 30s in total.
        SECONDS=0
        while [ "$SECONDS" -lt 5 ]; do
            case "$(_probe 0.5)" in ours*) ;; *) break ;; esac
            sleep 0.25
        done
        case "$(_probe 0.5)" in
            ours*)
                log "ERROR: the old proxy is still serving on :$PORT and could not be stopped — not starting a second one."
                exit 0
                ;;
        esac
        ;;
    foreign)
        # Starting here would only fail to bind, silently, forever. Say so.
        log "ERROR: port $PORT is held by something that is not this proxy — not starting. Free the port or set ROLLING_CONTEXT_PORT to another one."
        exit 0
        ;;
    *)
        [ -f "$PIDFILE" ] && log "PID file present but nothing is serving on :$PORT — the recorded proxy is gone; restarting"
        _clear_recorded_proxy
        ;;
esac

# Start proxy directly — no venv needed (pure stdlib)
if [ ! -f "$PROXY_DIR/server.py" ]; then
    log "ERROR: $PROXY_DIR/server.py not found — proxy not started"
    exit 0
fi
log "Starting proxy..."
(
    cd "$PROXY_DIR" || exit 1
    nohup $PYTHON_CMD server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
)

# Confirm it came up, rather than assuming. A start that fails — port taken,
# python missing, a syntax error in the proxy — used to be invisible: the hook
# exited 0, the session was already pointed at this port, and the only symptom
# was ConnectionRefused on every request, with nothing in this log to explain
# it. Bounded at ~8s so a cold start never stalls the session for long.
SECONDS=0
while [ "$SECONDS" -lt 8 ]; do
    case "$(_probe 0.5)" in
        "ours $CURRENT_VERSION")
            # Record the PID /health reports, not $! — under git-bash on
            # Windows $! is an MSYS job number that Get-Process/Stop-Process
            # know nothing about, and if two sessions started at once the
            # process that actually won the bind may not be ours at all.
            _pid=$(_probe_pid 0.5)
            echo "$_pid" > "$PIDFILE"
            echo "$CURRENT_VERSION" > "$VERFILE"
            log "Proxy healthy on :$PORT (v$CURRENT_VERSION, PID $_pid)"
            exit 0
            ;;
    esac
    sleep 0.25
done
log "ERROR: proxy did not answer on :$PORT within ~8s of starting — see $HOME/.claude/rolling-context-proxy.log"

exit 0
