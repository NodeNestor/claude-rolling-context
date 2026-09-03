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

# Wire ourselves into Claude Code via HTTPS_PROXY + NODE_EXTRA_CA_CERTS (NOT
# ANTHROPIC_BASE_URL, which trips the Remote Control / GrowthBook gate). CA
# generation, HTTPS_PROXY single-owner ownership, chaining with pii-proxy,
# plugin defaults and stale-base_url cleanup all live in wire.py — one tested
# implementation shared with the PowerShell hook and with pii-proxy.
SETTINGS_FILE="$HOME/.claude/settings.json"
WIRE_OUT=$($PYTHON_CMD "$PROXY_DIR/wire.py" --name rolling-context --settings "$SETTINGS_FILE" 2>&1)
if [ $? -eq 0 ]; then
    while IFS= read -r line; do [ -n "$line" ] && log "wire:$line"; done <<< "$WIRE_OUT"
else
    log "WARNING: wire.py failed to update settings.json: $WIRE_OUT"
fi

# Check if proxy is already running
_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        # TERM first: proxies from 1.13.2 on drain their in-flight requests
        # before exiting. Escalate to KILL only once that grace is used up.
        kill "$pid" 2>/dev/null
        local waited=0
        while [ "$waited" -lt 24 ] && kill -0 "$pid" 2>/dev/null; do
            sleep 0.25; waited=$((waited + 1))
        done
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

_decide() {
    # down | foreign | same | newer <v> | older <v> idle | older <v> busy <n> <how>
    $PYTHON_CMD "$SCRIPT_DIR/probe.py" "$PORT" "${1:-2}" decide "$CURRENT_VERSION" 2>/dev/null
}

_wait_until_idle() {
    # Poll the decision until the running proxy has nothing in flight, for
    # at most $1 seconds. Prints the final decision.
    local budget="$1" d
    SECONDS=0
    while :; do
        d=$(_decide 0.5)
        case "$d" in "older "*" busy "*) ;; *) break ;; esac
        [ "$SECONDS" -ge "$budget" ] && break
        sleep 0.5
    done
    echo "$d"
}

_replace_running_proxy() {
    _clear_recorded_proxy serving
    # The old proxy owns the port until it actually exits. Starting on top
    # of it would just lose the bind and leave the old version serving
    # while the log claimed a restart.
    # Bounded by the clock, not by a probe count: each probe costs an
    # interpreter start, and the SessionStart hook has 30s in total.
    SECONDS=0
    while [ "$SECONDS" -lt 5 ]; do
        case "$(_probe 0.5)" in ours*) ;; *) return 0 ;; esac
        sleep 0.25
    done
    case "$(_probe 0.5)" in
        ours*)
            log "ERROR: the old proxy is still serving on :$PORT and could not be stopped — not starting a second one."
            exit 0
            ;;
    esac
}

# Ask the PORT, not the PID file. The PID file only records an intention to
# run; the port is where "running" is either true or it is not. See probe.py
# and issue #9 for the failure this replaces.
# The proxy is shared by every Claude Code session on this machine, and after
# a plugin auto-update sessions on the old version and sessions on the new one
# each run THIS hook from their own plugin cache dir. "Version differs, so
# restart" made the two cohorts take turns killing the proxy, and every kill
# cut every session's in-flight stream (nestor-plugins issue #1). Policy now:
# never downgrade, and only replace an older proxy when nothing is in flight.
# ROLLING_CONTEXT_FORCE_RESTART=1 (the /rolling-context:restart command)
# restarts regardless of version, still waiting briefly for idle.
FORCE="${ROLLING_CONTEXT_FORCE_RESTART:-}"
DECISION=$(_decide 2)
case "$DECISION" in
    same)
        if [ -n "$FORCE" ]; then
            DECISION=$(_wait_until_idle 6)
            log "Forced restart of v$CURRENT_VERSION (${DECISION})"
            _replace_running_proxy
        else
            log "Proxy already running and healthy on :$PORT (v$CURRENT_VERSION)"
            exit 0
        fi
        ;;
    newer\ *)
        if [ -n "$FORCE" ]; then
            DECISION=$(_wait_until_idle 6)
            log "Forced restart: replacing v${DECISION#newer } with v$CURRENT_VERSION"
            _replace_running_proxy
        else
            log "Proxy v${DECISION#newer } on :$PORT is newer than this plugin (v$CURRENT_VERSION) — leaving it alone, never downgrading. This session uses it as is."
            exit 0
        fi
        ;;
    older\ *)
        set -- $DECISION   # older <v> idle | older <v> busy <n> <how>
        RUNNING="$2"
        if [ "$3" = "busy" ]; then
            log "Proxy v$RUNNING is older than v$CURRENT_VERSION but has $4 in flight ($5) — waiting for it to go idle before upgrading"
            DECISION=$(_wait_until_idle 6)
            set -- $DECISION
        fi
        case "$DECISION" in
            "older "*" idle"|"older "*" busy "*)
                if [ "$3" = "busy" ] && [ -z "$FORCE" ]; then
                    log "Deferring upgrade: proxy v$RUNNING still has $4 in flight ($5). This session uses it as is; the upgrade to v$CURRENT_VERSION happens at the next session start that finds it idle, or now via /rolling-context:restart."
                    exit 0
                fi
                log "Upgrading proxy v$RUNNING -> v$CURRENT_VERSION (${3:-idle}${FORCE:+, forced})"
                _replace_running_proxy
                ;;
            same|newer\ *)
                # Someone else finished the upgrade while we waited.
                log "Proxy on :$PORT was upgraded by another session while we waited ($DECISION)"
                exit 0
                ;;
            *)
                log "Proxy v$RUNNING went away while we waited ($DECISION); starting v$CURRENT_VERSION"
                _clear_recorded_proxy
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
