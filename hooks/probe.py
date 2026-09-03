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

    python probe.py [port] [timeout_seconds] decide <our_version>

is the whole upgrade policy in one line of output (nestor-plugins issue #1 —
two plugin versions installed side by side took turns restarting the shared
proxy on "version differs", cutting every session's in-flight stream each
time):

    down                       nothing is serving — start ours
    foreign                    something else holds the port — do not start
    same                       our version is serving — leave it alone
    newer <v>                  a NEWER version is serving — leave it alone;
                               the hook never downgrades
    older <v> idle             an older version is serving with nothing in
                               flight — replace it now
    older <v> busy <n> <how>   an older version is serving <n> requests
                               (how=requests: it told us via /health;
                               how=sockets: too old to say, so we counted
                               established client connections instead) —
                               wait, and if it never goes idle, defer the
                               upgrade to a later session start
"""
import json
import os
import re
import subprocess
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


_NUM = re.compile(r"\d+")


def version_key(v):
    """Comparable form of a version string: numeric dotted parts only.

    "1.13.1" -> (1, 13, 1); "1.13.1-rc2" -> (1, 13, 1); "legacy", "unknown"
    and anything without digits -> () which sorts below every real version,
    so the hook treats it as older and replaces it.
    """
    core = str(v or "").split("-", 1)[0].split("+", 1)[0]
    return tuple(int(n) for n in _NUM.findall(core))


def compare(running, ours):
    """-1 if the running version is older than ours, 0 if same, 1 if newer."""
    a, b = version_key(running), version_key(ours)
    if a == b:
        return 0
    return -1 if a < b else 1


def established_client_sockets(ports):
    """Established TCP connections whose LOCAL side is one of `ports`.

    The fallback for proxies that predate the active_requests field: a client
    talking to us shows up as ESTABLISHED on our listening port. `netstat -an`
    exists on macOS, Linux and Windows with formats that all put the local
    address in a column ending in :PORT or .PORT. None if netstat is missing.
    """
    ports = {str(p) for p in ports}
    cmds = (["netstat", "-an"], ["ss", "-tan"])
    out = None
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            if out:
                break
        except Exception:
            continue
    if not out:
        return None
    n = 0
    for line in out.splitlines():
        u = line.upper()
        if "ESTAB" not in u:
            continue
        cols = line.split()
        # local address is the first column that looks like an endpoint
        # (netstat: after "tcp4"/"TCP"; ss: recv/send-q columns come first)
        for col in cols:
            m = re.search(r"[:.](\d+)$", col)
            if m:
                if m.group(1) in ports:
                    n += 1
                break
    return n


def decide(body, ours, port):
    st = state(body)
    if st in ("down", "foreign"):
        return st
    running = "legacy" if st == "ours legacy" else str(body.get("version") or "unknown")
    rel = compare(running, ours)
    if rel == 0:
        return "same"
    if rel > 0:
        return f"newer {running}"
    active = body.get("active_requests") if isinstance(body, dict) else None
    if isinstance(active, int):
        return f"older {running} idle" if active == 0 else f"older {running} busy {active} requests"
    mitm = os.environ.get("ROLLING_CONTEXT_MITM_PORT") or "5590"
    n = established_client_sockets([port, mitm])
    if n is None or n == 0:
        return f"older {running} idle"
    return f"older {running} busy {n} sockets"


if __name__ == "__main__":
    _port = sys.argv[1] if len(sys.argv) > 1 else "5588"
    try:
        _timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    except (IndexError, ValueError):
        _timeout = 2.0
    _mode = sys.argv[3] if len(sys.argv) > 3 else ""
    _body = health(_port, _timeout)
    if _mode == "pid":
        print((_body or {}).get("pid") or "")
    elif _mode == "decide":
        print(decide(_body, sys.argv[4] if len(sys.argv) > 4 else "unknown", _port))
    else:
        print(state(_body))
