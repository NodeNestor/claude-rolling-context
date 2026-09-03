"""
Shared MITM front-end for the body-rewriting Claude Code proxies
(rolling-context, pii-proxy).

Why this exists
---------------
These plugins rewrite request/response bodies. Historically they took the
``ANTHROPIC_BASE_URL`` slot so Claude Code would POST to them in plaintext.
But a non-anthropic ``ANTHROPIC_BASE_URL`` host trips Claude Code's Remote
Control / GrowthBook gate. Routing through ``HTTPS_PROXY`` instead leaves the
destination as ``api.anthropic.com`` (gate untouched) — but then the traffic
is TLS end-to-end and the body is opaque.

This module bridges the gap: it terminates TLS for ``api.anthropic.com`` with a
locally generated CA (trusted by Claude Code via ``NODE_EXTRA_CA_CERTS``), then
hands the decrypted, plaintext HTTP connection to the plugin's *existing*
``BaseHTTPRequestHandler`` — so all body-rewriting, streaming and chaining code
is reused untouched. Every other CONNECT host is blind-tunnelled unchanged.

The same file ships verbatim in both plugins.
"""
from __future__ import annotations

import datetime
import os
import select
import socket
import ssl
import threading

# Hosts intercepted by default; everything else is blind-tunnelled so telemetry,
# updates and any unrelated TLS pass through untouched. A non-Anthropic upstream
# (a custom gateway configured via ANTHROPIC_BASE_URL) is added at runtime so it
# is intercepted too — see ensure_ca(extra_hosts=...) and serve(hosts=...).
MITM_HOSTS = {"api.anthropic.com"}

_CA_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# CA + leaf generation
# --------------------------------------------------------------------------
def ensure_ca(ca_dir: str, extra_hosts=()):
    """Generate (once) a local CA and a leaf cert covering the MITM hosts.

    Returns (ca_cert_path, server_ssl_context, hosts). `hosts` is the full set
    of intercepted hostnames (defaults plus extra_hosts). The CA cert is what
    the caller advertises to Claude Code via NODE_EXTRA_CA_CERTS. Idempotent and
    process-safe: the CA is generated once; the leaf is regenerated only when it
    does not yet cover every requested host.
    """
    os.makedirs(ca_dir, exist_ok=True)
    ca_cert_path = os.path.join(ca_dir, "ca-cert.pem")
    ca_key_path = os.path.join(ca_dir, "ca-key.pem")
    leaf_path = os.path.join(ca_dir, "leaf.pem")  # cert + key, PEM bundle
    hosts_path = os.path.join(ca_dir, "leaf-hosts.txt")

    hosts = set(MITM_HOSTS) | {h.strip().lower() for h in extra_hosts if h and h.strip()}

    with _CA_LOCK:
        have_ca = os.path.exists(ca_cert_path) and os.path.exists(ca_key_path)
        covered = set()
        if os.path.exists(hosts_path):
            try:
                with open(hosts_path, encoding="utf-8") as f:
                    covered = {ln.strip().lower() for ln in f if ln.strip()}
            except OSError:
                covered = set()
        if not have_ca or not os.path.exists(leaf_path) or not hosts <= covered:
            _generate(ca_cert_path, ca_key_path, leaf_path, hosts_path, sorted(hosts),
                      reuse_ca=have_ca)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(leaf_path)
    return ca_cert_path, ctx, hosts


def _generate(ca_cert_path: str, ca_key_path: str, leaf_path: str,
              hosts_path: str, hosts, reuse_ca: bool = False) -> None:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    def _atomic_write(path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Claude Local Proxy CA")])

    if reuse_ca:
        # Only the leaf needs to change (new host added) — keep the CA so the CA
        # already trusted via NODE_EXTRA_CA_CERTS stays valid.
        with open(ca_key_path, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name).issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, key_encipherment=False,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        _atomic_write(ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM))
        _atomic_write(ca_key_path, ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(h) for h in hosts]
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    _atomic_write(leaf_path,
                  leaf_cert.public_bytes(serialization.Encoding.PEM)
                  + leaf_key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    _atomic_write(hosts_path, ("\n".join(hosts) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# CONNECT proxy
# --------------------------------------------------------------------------
def _read_connect_line(sock: socket.socket):
    """Read the CONNECT request line + headers. Returns (host, port) or None."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            return None
    first = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = first.split(" ")
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    hostport = parts[1]
    host, _, port = hostport.partition(":")
    return host, int(port or "443")


def _blind_tunnel(client: socket.socket, host: str, port: int) -> None:
    try:
        upstream = socket.create_connection((host, port), timeout=30)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        return
    socks = [client, upstream]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 300)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (upstream if s is client else client).sendall(data)
    except OSError:
        pass
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def serve(mitm_port: int, ca_dir: str, on_terminated, log=None, host="127.0.0.1",
          extra_hosts=()):
    """Start the CONNECT front-end. Blocks; run in a thread.

    on_terminated(tls_sock, client_addr, sni_host) is called with a decrypted,
    server-side TLS socket for each intercepted connection. It owns the socket.
    extra_hosts adds non-default hostnames to intercept (e.g. a custom gateway).
    """
    try:
        ca_cert_path, ctx, hosts = ensure_ca(ca_dir, extra_hosts)
    except ImportError:
        if log:
            log("MITM front-end NOT started: the 'cryptography' package is missing, "
                "so the local CA cannot be generated. Install it in this proxy's "
                "environment (it is in requirements.txt). HTTPS_PROXY entrypoint is disabled.")
        return
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"MITM front-end NOT started: CA setup failed ({e!r}). "
                "HTTPS_PROXY entrypoint is disabled.")
        return
    if log:
        log(f"MITM front-end: CA at {ca_cert_path}, intercepting {sorted(hosts)}")

    global _listener
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, mitm_port))
    srv.listen(128)
    _listener = srv
    if log:
        log(f"MITM front-end listening on {host}:{mitm_port}")

    def handle(client, addr):
        try:
            target = _read_connect_line(client)
            if target is None:
                client.close()
                return
            thost, tport = target
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if thost.lower() in hosts:
                try:
                    tls = ctx.wrap_socket(client, server_side=True)
                except ssl.SSLError as e:
                    if log:
                        log(f"TLS handshake failed for {thost}: {e}")
                    client.close()
                    return
                on_terminated(tls, addr, thost)
            else:
                _blind_tunnel(client, thost, tport)
        except Exception as e:  # noqa: BLE001 - never let one conn kill the loop
            if log:
                log(f"MITM conn error: {e!r}")
            try:
                client.close()
            except OSError:
                pass

    while True:
        try:
            client, addr = srv.accept()
        except OSError:
            # Listener closed by close_listener() during a drain.
            return
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


_listener = None


def close_listener():
    """Stop accepting CONNECTs (drain). Established tunnels are untouched."""
    global _listener
    srv, _listener = _listener, None
    if srv is not None:
        try:
            srv.close()
        except OSError:
            pass


def start_in_thread(mitm_port: int, ca_dir: str, on_terminated, log=None,
                    extra_hosts=()) -> threading.Thread:
    t = threading.Thread(target=serve,
                         args=(mitm_port, ca_dir, on_terminated, log, "127.0.0.1", extra_hosts),
                         daemon=True)
    t.start()
    return t
