#!/usr/bin/env python3
"""Regression suite for issue #12 — a transport flip must not silently strand
already-running sessions, and a pre-CONNECT close must never be silent.

Two halves, no framework:

  A. wire.py main() defers the ANTHROPIC_BASE_URL -> HTTPS_PROXY+MITM flip while
     sessions are established on our ports (their CA trust is fixed at launch, so
     the flip would ECONNRESET them). It performs the flip once idle, never
     defers a no-op re-wire, and honours ROLLING_CONTEXT_FORCE_WIRE.
  B. mitm_frontend._read_connect_line reports a reason instead of a bare None, so
     the front-end logs every connection it closes before a usable CONNECT.

  python tests/test_transport_flip.py
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
sys.path.insert(0, PROXY)

import wire  # noqa: E402
import mitm_frontend  # noqa: E402

FAILURES = []


def check(cond, label, detail=""):
    print(f"  {'ok ' if cond else 'FAIL'} {label}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def run_wire(env, live, force=False):
    """Run wire.main() over a settings.json holding `env`, with the live-socket
    count stubbed to `live`. Returns the env block written back."""
    with tempfile.TemporaryDirectory() as d:
        settings_path = os.path.join(d, "settings.json")
        ca_dir = os.path.join(d, "proxy-ca")
        os.makedirs(ca_dir, exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({"env": dict(env)}, f)

        orig_sockets = wire._established_client_sockets
        orig_argv = sys.argv
        orig_stdout = sys.stdout
        orig_force = os.environ.get("ROLLING_CONTEXT_FORCE_WIRE")
        wire._established_client_sockets = lambda ports: live
        sys.argv = ["wire.py", "--name", "rolling-context",
                    "--settings", settings_path, "--ca-dir", ca_dir]
        if force:
            os.environ["ROLLING_CONTEXT_FORCE_WIRE"] = "1"
        else:
            os.environ.pop("ROLLING_CONTEXT_FORCE_WIRE", None)
        sys.stdout = io.StringIO()
        try:
            wire.main()
            out = sys.stdout.getvalue()
        finally:
            wire._established_client_sockets = orig_sockets
            sys.argv = orig_argv
            sys.stdout = orig_stdout
            if orig_force is None:
                os.environ.pop("ROLLING_CONTEXT_FORCE_WIRE", None)
            else:
                os.environ["ROLLING_CONTEXT_FORCE_WIRE"] = orig_force
        with open(settings_path, encoding="utf-8") as f:
            return json.load(f)["env"], out


PRE_113 = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588"}
CA = "/home/u/.claude/proxy-ca/ca-cert.pem"
WIRED_113 = {"HTTPS_PROXY": "http://127.0.0.1:5590", "NODE_EXTRA_CA_CERTS": CA}


def test_wire():
    print("A. wire.py transport-flip deferral")

    # Flip pending + sessions live -> deferred, file untouched.
    env, out = run_wire(PRE_113, live=3)
    check(env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:5588",
          "deferred: base_url left in place", env)
    check("HTTPS_PROXY" not in env, "deferred: HTTPS_PROXY not added", env)
    check("DEFERRED" in out, "deferred: warning printed")

    # Flip pending + idle -> performed.
    env, out = run_wire(PRE_113, live=0)
    check(env.get("HTTPS_PROXY") == "http://127.0.0.1:5590",
          "idle: HTTPS_PROXY set", env)
    check("ANTHROPIC_BASE_URL" not in env, "idle: stale base_url removed", env)
    check(env.get("NODE_EXTRA_CA_CERTS"), "idle: CA wired", env)
    check("DEFERRED" not in out, "idle: no deferral warning")

    # Force overrides the live check.
    env, out = run_wire(PRE_113, live=3, force=True)
    check(env.get("HTTPS_PROXY") == "http://127.0.0.1:5590",
          "forced: flip performed despite live sessions", env)

    # Steady state (already wired) is a no-op re-wire, never deferred even busy.
    env, out = run_wire(WIRED_113, live=9)
    check(env.get("HTTPS_PROXY") == "http://127.0.0.1:5590",
          "steady: HTTPS_PROXY preserved", env)
    check("DEFERRED" not in out, "steady: no deferral on a no-op re-wire")

    # The predicate itself.
    check(wire._reaches_via_our_mitm(WIRED_113, 5590) is True, "predicate: wired reaches mitm")
    check(wire._reaches_via_our_mitm(PRE_113, 5590) is False, "predicate: pre-1.13 does not")
    check(wire._reaches_via_our_mitm({"HTTPS_PROXY": "http://127.0.0.1:5590"}, 5590) is False,
          "predicate: HTTPS_PROXY without CA does not reach")


class FakeSock:
    """A socket whose recv() yields the queued chunks then EOF."""
    def __init__(self, *chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


def test_connect_reasons():
    print("B. mitm_frontend._read_connect_line reasons")

    host, port = mitm_frontend._read_connect_line(
        FakeSock(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"))
    check((host, port) == ("api.anthropic.com", 443), "valid CONNECT parsed", (host, port))

    host, reason = mitm_frontend._read_connect_line(FakeSock())
    check(host is None and "before sending any request" in reason,
          "empty connection -> reason", reason)

    host, reason = mitm_frontend._read_connect_line(FakeSock(b"\x16\x03\x01\x00\xa5" + b"\x00" * 60))
    check(host is None and "TLS directly" in reason,
          "TLS ClientHello at proxy port -> reason", reason)

    host, reason = mitm_frontend._read_connect_line(
        FakeSock(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"))
    check(host is None and "not a CONNECT" in reason,
          "plain GET -> reason", reason)


if __name__ == "__main__":
    test_wire()
    test_connect_reasons()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        raise SystemExit(1)
    print("\nAll transport-flip checks passed.")
