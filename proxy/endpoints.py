"""Where the proxy sends traffic — resolved once, shared by every consumer.

The start hook writes `ROLLING_CONTEXT_UPSTREAM` into `settings.json` but does
not export it into this process, so reading only `os.environ` silently loses a
custom endpoint. `server.py` worked around that (issue #3) while
`compressor.py` kept its own env-only lookup, which sent every background
compaction to api.anthropic.com carrying the custom endpoint's credentials —
a permanent 401, a permanent cooldown, and a session that never actually gets
compressed (issue #5). Both now resolve through here.
"""
import json
import logging
import os
import ssl
from urllib.parse import urlparse

log = logging.getLogger("rolling-context.endpoints")

DEFAULT_UPSTREAM = "https://api.anthropic.com"

LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT") or "5588")


def outbound_ssl_context() -> ssl.SSLContext:
    """SSL context for the proxy's OUTBOUND TLS to the upstream.

    ``ssl.create_default_context()`` trusts only the host's OS CA store, which on
    some Python installs is empty — notably a python.org macOS build where
    ``Install Certificates.command`` was never run: the ``etc/openssl/cert.pem``
    symlink is absent, the context loads zero roots, and every upstream handshake
    fails ``CERTIFICATE_VERIFY_FAILED``. Claude Code surfaces that as an opaque
    502 that reads exactly like an Anthropic outage (issue #13). certifi ships a
    CA bundle; add it so verification works regardless of the OS setup. This is a
    union (``load_verify_locations`` adds roots), so OS and corporate roots stay
    trusted too. certifi is optional (the proxy targets stdlib) — if it is not
    importable we keep the OS store and warn when it is empty, so the failure is
    diagnosable instead of silent.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception as e:  # noqa: BLE001 - certifi missing/broken is non-fatal
        log.warning("certifi CA bundle unavailable (%s); trusting only the OS CA "
                    "store for upstream TLS.", e)
    try:
        if ctx.cert_store_stats().get("x509_ca", 0) == 0:
            log.warning("Outbound TLS trust store is EMPTY: no OS CA bundle and no "
                        "certifi. Upstream requests will fail CERTIFICATE_VERIFY_FAILED "
                        "(this is the PROXY's TLS, not an Anthropic outage). Fix: "
                        "`pip install certifi`, or run this Python's "
                        "Install Certificates.command, then restart the proxy.")
    except Exception:  # noqa: BLE001 - cert_store_stats is best-effort
        pass
    return ctx


def annotate_upstream_tls_error(err) -> str:
    """If `err` is an outbound cert-verification failure, append the actionable
    cause so a viewer does not mistake it for an Anthropic-side 502 (issue #13)."""
    s = str(err)
    if isinstance(err, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in s:
        return (s + " -- this is the rolling-context proxy's OWN outbound TLS to the "
                "upstream failing certificate verification, NOT an Anthropic outage. "
                "The Python running the proxy has no usable CA bundle. Fix: "
                "`pip install certifi` (or run this Python's Install "
                "Certificates.command), then restart the proxy.")
    return s


def settings_env() -> dict:
    """The `env` block of ~/.claude/settings.json, or {} if unreadable.

    Decoded as utf-8-sig rather than utf-8. Windows PowerShell 5.1's
    `Set-Content -Encoding UTF8` — which the start hook used until v1.11.3 —
    writes a UTF-8 BOM, and Python's plain utf-8 codec rejects it outright
    ("Unexpected UTF-8 BOM"). That made this return {} on every Windows box,
    silently dropping a custom upstream and sending compaction to
    api.anthropic.com bearing the custom endpoint's key: issues #3 and #5
    again, through a different door. utf-8-sig decodes BOM-less files
    identically, so it is a strict superset and repairs configs already
    written with a BOM.
    """
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            return (json.load(f) or {}).get("env", {}) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # Never fail silently here. settings.json is the only carrier for a
        # custom upstream (the hook writes it there and does not export it),
        # so an unreadable file is indistinguishable from "no custom endpoint"
        # — which is exactly what made #3 and #5 so hard to find.
        log.warning(
            "Could not read %s (%s: %s) — falling back to %s. "
            "A custom endpoint configured there will be ignored.",
            path, type(e).__name__, e, DEFAULT_UPSTREAM,
        )
        return {}


def load_upstream(listen_port: int = LISTEN_PORT) -> str:
    """Resolve the upstream API endpoint.

    Env first, then settings.json, then a custom ANTHROPIC_BASE_URL — but never
    our own listen port, which would loop.
    """
    up = os.environ.get("ROLLING_CONTEXT_UPSTREAM")
    if up:
        return up

    env = settings_env()
    up = env.get("ROLLING_CONTEXT_UPSTREAM")
    if up:
        return up

    base = env.get("ANTHROPIC_BASE_URL", "")
    if base and (urlparse(base).port or 0) != listen_port:
        return base

    return DEFAULT_UPSTREAM


def is_anthropic(url: str) -> bool:
    """True for api.anthropic.com (and subdomains) — i.e. first-party."""
    host = (urlparse(url).hostname or "").lower()
    return host == "api.anthropic.com" or host.endswith(".anthropic.com")
