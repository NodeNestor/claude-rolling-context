#!/usr/bin/env python3
"""Coverage for wire.py — the HTTPS_PROXY / NODE_EXTRA_CA_CERTS bookkeeping that
replaced the old ANTHROPIC_BASE_URL chaining. Runnable directly:

    python tests/test_wire.py

Exits non-zero on the first failed assertion. No pytest needed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import wire  # noqa: E402

CA = "/x/proxy-ca/ca-cert.pem"
ROLL_MITM = "http://127.0.0.1:5590"
PII_MITM = "http://127.0.0.1:5601"
ROLL_CORE = "http://127.0.0.1:5588"
PII_CORE = "http://127.0.0.1:5599"
API = "https://api.anthropic.com"

_fails = 0


def check(label, cond, extra=""):
    global _fails
    if cond:
        print(f"  ok  {label}")
    else:
        _fails += 1
        print(f"  FAIL {label} {extra}")


def w(name, env):
    wire.wire_env(name, env, CA)
    return env


def u(name, env):
    wire.unwire_env(name, env, CA)
    return env


def main():
    # 1. rolling alone
    e = w("rolling-context", {})
    check("rolling owns HTTPS_PROXY", e["HTTPS_PROXY"] == ROLL_MITM, e)
    check("rolling upstream -> api", e["ROLLING_CONTEXT_UPSTREAM"] == API)
    check("NEC set", e["NODE_EXTRA_CA_CERTS"] == CA)
    check("defaults seeded", e.get("ROLLING_CONTEXT_TRIGGER") == "100000")

    # 2. + pii becomes inner
    w("pii-proxy", e)
    check("pii does not steal HTTPS_PROXY", e["HTTPS_PROXY"] == ROLL_MITM)
    check("rolling core -> pii core", e["ROLLING_CONTEXT_UPSTREAM"] == PII_CORE)
    check("pii core -> api", e["PII_PROXY_UPSTREAM"] == API)

    # 3. idempotent re-wire keeps the sibling chained
    w("rolling-context", e)
    check("re-wire keeps pii inner", e["ROLLING_CONTEXT_UPSTREAM"] == PII_CORE)

    # 4. pii first, then rolling -> rolling inner
    e2 = w("rolling-context", w("pii-proxy", {}))
    check("pii outer when wired first", e2["HTTPS_PROXY"] == PII_MITM)
    check("pii core -> rolling core", e2["PII_PROXY_UPSTREAM"] == ROLL_CORE)
    check("rolling core -> api", e2["ROLLING_CONTEXT_UPSTREAM"] == API)

    # 5. stale base_url pointing at us is dropped
    e3 = w("rolling-context", {"ANTHROPIC_BASE_URL": ROLL_CORE})
    check("stale base_url removed", "ANTHROPIC_BASE_URL" not in e3)

    # 6. external gateway via base_url is kept and intercepted
    e4 = w("rolling-context", {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1"})
    check("gateway base_url kept", e4["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api/v1")
    check("gateway is terminal upstream", e4["ROLLING_CONTEXT_UPSTREAM"] == "https://openrouter.ai/api/v1")
    e4b = w("pii-proxy", e4)
    check("gateway reaches innermost hop", e4b["PII_PROXY_UPSTREAM"] == "https://openrouter.ai/api/v1")

    # 7. external corporate HTTPS_PROXY is left alone
    e5 = w("rolling-context", {"HTTPS_PROXY": "http://corp:8080"})
    check("corporate HTTPS_PROXY untouched", e5["HTTPS_PROXY"] == "http://corp:8080")

    # 8. unwire outer promotes the sibling
    both = w("pii-proxy", w("rolling-context", {}))
    u("rolling-context", both)
    check("unwire outer promotes pii", both["HTTPS_PROXY"] == PII_MITM)
    check("pii keeps CA", both["NODE_EXTRA_CA_CERTS"] == CA)
    check("rolling upstream removed", "ROLLING_CONTEXT_UPSTREAM" not in both)

    # 9. unwire inner bypasses it
    both2 = w("pii-proxy", w("rolling-context", {}))
    u("pii-proxy", both2)
    check("rolling stays outer", both2["HTTPS_PROXY"] == ROLL_MITM)
    check("rolling now -> api", both2["ROLLING_CONTEXT_UPSTREAM"] == API)
    check("pii upstream removed", "PII_PROXY_UPSTREAM" not in both2)

    # 10. solo wire/unwire round-trip leaves nothing
    e6 = u("rolling-context", w("rolling-context", {}))
    check("solo unwire drops HTTPS_PROXY", "HTTPS_PROXY" not in e6)
    check("solo unwire drops NEC", "NODE_EXTRA_CA_CERTS" not in e6)

    print()
    if _fails:
        print(f"FAILED: {_fails} check(s)")
        return 1
    print("All wire.py checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
