#!/usr/bin/env python3
"""Real multi-session e2e for the issue #12 fix. No stubs:

 - a real proxy server.py subprocess (custom ports, temp HOME, mock upstream)
 - a real session held open on the core port (ESTABLISHED, seen by real netstat)
 - the real wire.py deciding defer-vs-flip from that real socket
 - the real MITM front-end: a real untrusted-CA client (a stranded session) and
   a real trusted-CA client through it.
"""
import json, os, socket, ssl, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(REPO, "proxy")
PY = sys.executable
try:
    import cryptography  # noqa: F401
except Exception:
    print("SKIP: cryptography not installed in this interpreter; "
          "the MITM CA cannot be generated."); sys.exit(0)

FAIL = []
def ok(c, label, detail=""):
    print(f"  {'ok ' if c else 'FAIL'} {label}" + (f"   ({detail})" if detail and not c else ""))
    if not c: FAIL.append(label)

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def wait_port(port, t=15):
    end = time.time() + t
    while time.time() < end:
        try: socket.create_connection(("127.0.0.1", port), 0.3).close(); return True
        except OSError: time.sleep(0.2)
    return False

def health(port):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"http://127.0.0.1:{port}/health", timeout=3) as r:
        return r.status, json.loads(r.read())

# ---- mock upstream (stands in for api.anthropic.com) ----------------------
class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _reply(self):
        b = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            if n: self.rfile.read(n)
        except Exception: pass
        self._reply()
    do_POST = do_GET

def main():
    home = tempfile.mkdtemp(prefix="rc_e2e_")
    os.makedirs(os.path.join(home, ".claude", "proxy-ca"), exist_ok=True)
    core, mitm, mockp = free(), free(), free()

    mock = ThreadingHTTPServer(("127.0.0.1", mockp), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    settings_path = os.path.join(home, ".claude", "settings.json")
    ca_dir = os.path.join(home, ".claude", "proxy-ca")
    # A session launched on the OLD transport: base_url -> core, no HTTPS_PROXY/CA.
    old_env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{core}",
        "ROLLING_CONTEXT_PORT": str(core),
        "ROLLING_CONTEXT_MITM_PORT": str(mitm),
        "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mockp}",
    }
    json.dump({"env": dict(old_env)}, open(settings_path, "w"))

    env = dict(os.environ)
    env.update({"USERPROFILE": home, "HOME": home, "HOMEDRIVE": "", "HOMEPATH": "",
                "ROLLING_CONTEXT_PORT": str(core), "ROLLING_CONTEXT_MITM_PORT": str(mitm),
                "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mockp}"})
    srv = subprocess.Popen([PY, "server.py"], cwd=PROXY, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    logbuf = []
    threading.Thread(target=lambda: [logbuf.append(l) for l in srv.stdout], daemon=True).start()
    def logtext():
        extra = ""
        for f in ("rolling-context-debug.log", "rolling-context-proxy.log"):
            p = os.path.join(home, ".claude", f)
            if os.path.exists(p):
                extra += open(p, encoding="utf-8", errors="replace").read()
        return "".join(logbuf) + extra

    try:
        assert wait_port(core, 20), "core did not come up"
        assert wait_port(mitm, 20), "mitm did not come up"
        st, body = health(core)
        print(f"server up: core={core} mitm={mitm} mock={mockp}  /health={st} v{body.get('version')}")

        def run_wire():
            r = subprocess.run([PY, "wire.py", "--name", "rolling-context",
                                "--settings", settings_path, "--ca-dir", ca_dir],
                               cwd=PROXY, env=env, capture_output=True, text=True)
            return r.stdout + r.stderr, json.load(open(settings_path))["env"]

        print("\nPROOF 1 - real deferral driven by a real established session")
        # Session A: a real client holds the core port open (no request -> the
        # HTTP handler blocks reading the request line; stays ESTABLISHED).
        a = socket.create_connection(("127.0.0.1", core)); time.sleep(0.5)
        out, envw = run_wire()
        ok("DEFERRED" in out, "wire defers while a session is established", out.strip()[:120])
        ok(envw.get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{core}",
           "settings.json left on the old transport (base_url kept)", envw)
        ok("HTTPS_PROXY" not in envw, "no HTTPS_PROXY written while deferred", envw)
        # The promise: the old transport keeps serving, so live sessions and new
        # ones are unaffected while deferred.
        st2, _ = health(core)
        ok(st2 == 200, "core port still serves the old transport while deferred", st2)

        # Session A ends. Now the flip must happen.
        a.close(); time.sleep(1.5)
        out2, envw2 = run_wire()
        ok("DEFERRED" not in out2, "wire performs the flip once idle", out2.strip()[:120])
        ok(envw2.get("HTTPS_PROXY") == f"http://127.0.0.1:{mitm}", "HTTPS_PROXY now set", envw2)
        ok("ANTHROPIC_BASE_URL" not in envw2, "stale base_url removed", envw2)
        ok(bool(envw2.get("NODE_EXTRA_CA_CERTS")), "CA wired", envw2)

        ca_path = os.path.join(ca_dir, "ca-cert.pem")
        ok(os.path.exists(ca_path), "server generated the MITM CA", ca_path)

        print("\nPROOF 2 - the MITM front-end, real clients")
        before = len(logtext())

        # (a) Stranded session: a client that does NOT trust the MITM CA - exactly
        # a session started before NODE_EXTRA_CA_CERTS was wired in.
        c = socket.create_connection(("127.0.0.1", mitm), 3)
        c.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"); c.recv(200)
        strand_err = None
        try:
            ssl.create_default_context().wrap_socket(c, server_hostname="api.anthropic.com")
        except ssl.SSLError as e: strand_err = str(e)
        time.sleep(0.6)
        lt = logtext()
        ok(strand_err is not None and ("UNKNOWN_CA" in strand_err or "CERTIFICATE_VERIFY_FAILED" in strand_err),
           "untrusted client's TLS is rejected (as a real stranded session)", strand_err)
        ok("UNKNOWN_CA" in lt and "restart" in lt,
           "proxy now LOGS the strand with cause + restart hint (was silent)",
           lt[before:][-200:])

        # (b) Trusted session: a client that trusts the CA routes through the MITM
        # to the mock upstream - proving the new transport genuinely works.
        tctx = ssl.create_default_context(); tctx.load_verify_locations(ca_path)
        c2 = socket.create_connection(("127.0.0.1", mitm), 3)
        c2.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n"); c2.recv(200)
        tls = tctx.wrap_socket(c2, server_hostname="api.anthropic.com")
        tls.sendall(b"GET /v1/models HTTP/1.1\r\nHost: api.anthropic.com\r\nConnection: close\r\n\r\n")
        resp = b""
        try:
            while True:
                d = tls.recv(4096)
                if not d: break
                resp += d
        except Exception: pass
        ok(b" 200 " in resp.split(b"\r\n", 1)[0] or resp[:12].split(b" ")[1:2] == [b"200"],
           "trusted client is routed through the MITM to upstream (200)", resp[:80])

        # (c) TLS spoken straight at the proxy port is named, not silent.
        before2 = len(logtext())
        c3 = socket.create_connection(("127.0.0.1", mitm), 3)
        c3.sendall(b"\x16\x03\x01\x00\x50" + b"\x00" * 60); time.sleep(0.4); c3.close()
        time.sleep(0.4)
        ok("TLS directly" in logtext()[before2:], "TLS-at-proxy-port is logged, not silently dropped")

    finally:
        srv.terminate()
        try: srv.wait(timeout=5)
        except Exception: srv.kill()
        mock.shutdown()

    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: {FAIL}"); sys.exit(1)
    print("ALL REAL MULTI-SESSION CHECKS PASSED")

if __name__ == "__main__":
    main()
