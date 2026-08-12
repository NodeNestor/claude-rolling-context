#!/usr/bin/env python3
"""Ask the port who is listening on it. One implementation, both hooks.

The start hook used to decide "is the proxy running?" from the PID file: does
it exist, and is that PID alive. Both questions can answer yes while nothing
is serving. A crashed proxy leaves its PID file behind, and the kernel is free
to hand that number to an unrelated process — `kill -0` then reports "alive",
the hook logs "Proxy already running", and every session that follows is
pointed at ANTHROPIC_BASE_URL with no listener behind it: ConnectionRefused on
every request until a human deletes the file by hand (issue #9).

A PID is not the thing we care about. Whether the port answers, and whether
the thing answering is us, is. That is what this asks.

    python probe.py [port] [timeout_seconds]

prints exactly one of:

    ours <version>   our proxy is serving (version "legacy" = older than the
                     /health identity fields, so the hook restarts it)
    foreign          something answers on that port, but it is not us
    down             nothing is serving

    python probe.py [port] [timeout_seconds] pid

prints the PID the serving proxy reports for itself, or nothing. That is the
authoritative one — the PID file is a copy that can go stale, and an upgrade
still has to stop whatever is really holding the port.
"""
import json
import sys
import urllib.error
import urllib.request


def health(port, timeout=2.0):
    """The /health body as a dict, or None if the port did not give us one."""
    # ProxyHandler({}) — with http_proxy/all_proxy set (common on corporate
    # boxes) urlopen would route this loopback probe through the corporate
    # proxy, which cannot reach 127.0.0.1 and would report our own healthy
    # proxy as down. Ask the port directly, always.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        # An HTTP server is up but does not serve our /health.
        return {}
    except Exception:
        # Refused, reset, timed out, unparseable body — nothing usable there.
        return None
    return body if isinstance(body, dict) else {}


def state(body):
    if body is None:
        return "down"
    if body.get("service") == "rolling-context":
        return "ours " + str(body.get("version") or "unknown")
    # Proxies older than the service marker still answer /health with this
    # shape. They are ours, and reporting them as such is what lets the hook
    # replace them on upgrade instead of declaring the port hostile.
    if "trigger_tokens" in body and "summarizer_model" in body:
        return "ours legacy"
    return "foreign"


if __name__ == "__main__":
    _port = sys.argv[1] if len(sys.argv) > 1 else "5588"
    try:
        _timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    except (IndexError, ValueError):
        _timeout = 2.0
    _body = health(_port, _timeout)
    if len(sys.argv) > 3 and sys.argv[3] == "pid":
        print((_body or {}).get("pid") or "")
    else:
        print(state(_body))
