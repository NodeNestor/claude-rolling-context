#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Runs on SessionStart — must be fast, non-blocking

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$HOOKLOG"
}

log "Hook started. SCRIPT_DIR=$SCRIPT_DIR PROXY_DIR=$PROXY_DIR"
log "CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-not set}"

# Fast check: is proxy already running?
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        log "Proxy already running (PID $PID)"
        exit 0
    fi
    log "Stale PID file (PID $PID not running), removing"
    rm -f "$PIDFILE"
fi

# Ensure ANTHROPIC_BASE_URL is set for future sessions
if [ -z "$ANTHROPIC_BASE_URL" ]; then
    SHELL_RC=""
    if [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.bash_profile" ]; then
        SHELL_RC="$HOME/.bash_profile"
    fi
    if [ -n "$SHELL_RC" ] && ! grep -q "ANTHROPIC_BASE_URL" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# Rolling Context proxy for Claude Code" >> "$SHELL_RC"
        echo "export ANTHROPIC_BASE_URL=$PROXY_URL" >> "$SHELL_RC"
        log "Added ANTHROPIC_BASE_URL to $SHELL_RC"
    fi
elif ! echo "$ANTHROPIC_BASE_URL" | grep -q "127\.0\.0\.1.*$PORT"; then
    SHELL_RC=""
    if [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.bash_profile" ]; then
        SHELL_RC="$HOME/.bash_profile"
    fi
    if [ -n "$SHELL_RC" ]; then
        if ! grep -q "ROLLING_CONTEXT_UPSTREAM" "$SHELL_RC" 2>/dev/null; then
            echo "" >> "$SHELL_RC"
            echo "# Rolling Context proxy chaining" >> "$SHELL_RC"
            echo "export ROLLING_CONTEXT_UPSTREAM=$ANTHROPIC_BASE_URL" >> "$SHELL_RC"
        fi
        sed -i.bak "s|export ANTHROPIC_BASE_URL=.*|export ANTHROPIC_BASE_URL=$PROXY_URL|" "$SHELL_RC" 2>/dev/null
        log "Chaining: upstream=$ANTHROPIC_BASE_URL"
    fi
    export ROLLING_CONTEXT_UPSTREAM="$ANTHROPIC_BASE_URL"
else
    log "ANTHROPIC_BASE_URL already set to $ANTHROPIC_BASE_URL"
fi

# Start proxy in background — DO NOT WAIT
log "Launching background setup..."
(
    cd "$PROXY_DIR" || { log "ERROR: cannot cd to $PROXY_DIR"; exit 1; }
    log "[bg] Working dir: $(pwd)"
    if [ ! -d "venv" ]; then
        log "[bg] Creating venv..."
        python3 -m venv venv 2>/dev/null || python -m venv venv 2>/dev/null
        if [ ! -d "venv" ]; then
            log "[bg] ERROR: venv creation failed!"
            exit 1
        fi
        log "[bg] Installing requirements..."
        ./venv/bin/pip install -q -r requirements.txt 2>/dev/null
        log "[bg] Requirements installed"
    fi
    log "[bg] Starting proxy server..."
    nohup ./venv/bin/python server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
    log "[bg] Proxy started with PID $!"
) &

log "Background setup launched, hook exiting"
exit 0
