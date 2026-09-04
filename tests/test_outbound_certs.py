#!/usr/bin/env python3
"""Regression suite for issue #13 — the proxy's outbound TLS must verify even
when the host Python's OS CA store is empty (python.org macOS without
Install Certificates.command), by falling back to certifi.

Real TLS, no mocks of the handshake: a CA + leaf are generated (reusing the
proxy's own ensure_ca), a TLS server serves with the leaf, and a client built by
endpoints.outbound_ssl_context() connects while the OS-default store is forced
empty and certifi is pointed at that CA. The empty store alone must fail; the
augmented context must succeed.

  python tests/test_outbound_certs.py
"""
import os
import socket
import ssl
import sys
import tempfile
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

try:
    import mitm_frontend  # needs cryptography to mint the CA
    mitm_frontend.ensure_ca  # noqa: B018
    _ = __import__("cryptography")
except Exception:
    print("SKIP: cryptography not installed; cannot mint a test CA."); sys.exit(0)

import endpoints  # noqa: E402

FAIL = []
def ok(c, label, detail=""):
    print(f"  {'ok ' if c else 'FAIL'} {label}" + (f"   ({detail})" if detail and not c else ""))
    if not c: FAIL.append(label)


def empty_client_ctx(*a, **k):
    """A default-context stand-in with verification ON but ZERO roots — exactly
    the state create_default_context() lands in when the OS bundle is missing."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = True
    c.verify_mode = ssl.CERT_REQUIRED
    return c


def main():
    ca_dir = tempfile.mkdtemp(prefix="rc13_")
    ca_path, server_ctx, _hosts = mitm_frontend.ensure_ca(ca_dir, ["localhost"])

    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(4); port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                raw, _ = srv.accept()
            except OSError:
                return
            def h(raw):
                try:
                    t = server_ctx.wrap_socket(raw, server_side=True)
                    t.recv(1024)
                    t.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi")
                    t.close()
                except OSError:
                    pass
            threading.Thread(target=h, args=(raw,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()

    def get(ctx):
        s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), 3),
                            server_hostname="localhost")
        s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        data = s.recv(256); s.close(); return data

    # Force the "OS store is empty" world and point certifi at our CA.
    orig_default = ssl.create_default_context
    fake_certifi = types.ModuleType("certifi"); fake_certifi.where = lambda: ca_path
    sys.modules["certifi"] = fake_certifi
    ssl.create_default_context = empty_client_ctx
    try:
        print("issue #13 — outbound TLS with an empty OS CA store")

        # 1. The bug: an empty default store cannot verify -> the exact failure
        #    users saw as an opaque 502.
        empty_err = None
        try:
            get(empty_client_ctx())
        except ssl.SSLError as e:
            empty_err = str(e)
        ok(empty_err is not None and "CERTIFICATE_VERIFY_FAILED" in empty_err,
           "empty OS store fails CERTIFICATE_VERIFY_FAILED (reproduces the bug)", empty_err)

        # 2. The fix: outbound_ssl_context() augments with certifi and verifies.
        ctx = endpoints.outbound_ssl_context()
        ok(ctx.cert_store_stats().get("x509_ca", 0) > 0,
           "outbound_ssl_context() has roots despite the empty OS store")
        resp = None
        try:
            resp = get(ctx)
        except ssl.SSLError as e:
            resp = b"ERR:" + str(e).encode()
        ok(resp == b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nhi",
           "certifi-augmented context completes the handshake", resp)
    finally:
        ssl.create_default_context = orig_default
        sys.modules.pop("certifi", None)
        srv.close()

    print("\nannotate_upstream_tls_error")
    verify_err = ssl.SSLCertVerificationError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    msg = endpoints.annotate_upstream_tls_error(verify_err)
    ok("rolling-context proxy's OWN outbound TLS" in msg and "pip install certifi" in msg,
       "cert-verify error is annotated with the actionable cause")
    other = endpoints.annotate_upstream_tls_error(ConnectionResetError("boom"))
    ok(other == "boom", "unrelated errors are passed through unchanged", other)

    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: {FAIL}"); sys.exit(1)
    print("ALL ISSUE #13 CHECKS PASSED")


if __name__ == "__main__":
    main()
