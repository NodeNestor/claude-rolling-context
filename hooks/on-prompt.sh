#!/usr/bin/env bash
# Ensure rolling context proxy is running before each prompt
# Fast path: if proxy is already up, exits in <100ms

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"

# Fast check: is proxy already running?
if command -v curl &>/dev/null; then
    if curl -s --max-time 1 "$PROXY_URL/health" &>/dev/null; then
        exit 0
    fi
fi

# Check PID file
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
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
    fi
    export ROLLING_CONTEXT_UPSTREAM="$ANTHROPIC_BASE_URL"
fi

# Start venv setup + proxy in background
(
    cd "$PROXY_DIR" || exit 1
    if [ ! -d "venv" ]; then
        python3 -m venv venv 2>/dev/null || python -m venv venv 2>/dev/null
        ./venv/bin/pip install -q -r requirements.txt 2>/dev/null
    fi
    nohup ./venv/bin/python server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
) &

# Wait for proxy to be ready (up to 15s for first-time venv setup)
for i in $(seq 1 30); do
    sleep 0.5
    if curl -s --max-time 1 "$PROXY_URL/health" &>/dev/null; then
        exit 0
    fi
done

exit 0
