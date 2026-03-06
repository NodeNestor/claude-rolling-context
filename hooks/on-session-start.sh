#!/usr/bin/env bash
# Auto-start the rolling context proxy if it's not already running.
# Handles chaining: if ANTHROPIC_BASE_URL already points elsewhere, sets that as upstream.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"

# Handle ANTHROPIC_BASE_URL chaining
if [ -z "$ANTHROPIC_BASE_URL" ]; then
    # Not set — add to shell rc
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
    fi
elif ! echo "$ANTHROPIC_BASE_URL" | grep -q "127\.0\.0\.1.*$PORT"; then
    # Set to something else — chain through it
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
        # Replace the existing ANTHROPIC_BASE_URL with our proxy
        sed -i.bak "s|export ANTHROPIC_BASE_URL=.*|export ANTHROPIC_BASE_URL=$PROXY_URL|" "$SHELL_RC" 2>/dev/null
    fi
    export ROLLING_CONTEXT_UPSTREAM="$ANTHROPIC_BASE_URL"
fi

# Check if proxy is already running
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
    rm -f "$PIDFILE"
fi

# Check if something is already listening on the port
if command -v curl &>/dev/null; then
    if curl -s "$PROXY_URL/health" &>/dev/null; then
        exit 0
    fi
fi

# Start venv setup + proxy in background to avoid hook timeout
(
    cd "$PROXY_DIR" || exit 1
    if [ ! -d "venv" ]; then
        python3 -m venv venv 2>/dev/null || python -m venv venv 2>/dev/null
        ./venv/bin/pip install -q -r requirements.txt 2>/dev/null
    fi
    nohup ./venv/bin/python server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
) &

exit 0
