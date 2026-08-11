#!/usr/bin/env python3
"""Regression suite for issue #5 — custom endpoints must compact too.

Two halves, no framework needed:

  A. endpoint resolution — server.py and compressor.py must agree on where
     traffic goes, including when the endpoint only exists in settings.json
     (the hook writes it there and does not export it into this process).
  B. live proxy against a mock endpoint — two turns, asserting that background
     compaction reaches the endpoint and that the next turn goes upstream
     materially smaller than the conversation held locally. Run twice: once
     against a permissive endpoint, once against a strict one that rejects
     cache_control / tool_choice, which exercises the flattened fallback.

  python tests/test_custom_endpoint.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
MOCK = os.path.join(HERE, "mock_endpoint.py")

CUSTOM = "https://api.z.ai/api/anthropic"
ANTHROPIC = "https://api.anthropic.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def fake_home(root, settings_env, bom=False):
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    # bom=True reproduces what Windows PowerShell 5.1's `Set-Content -Encoding
    # UTF8` left behind (the start hook's own write, until v1.11.3). Only ever
    # writing BOM-less files is precisely why this suite stayed green while
    # every Windows user's custom endpoint was being silently ignored.
    encoding = "utf-8-sig" if bom else "utf-8"
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding=encoding) as f:
        json.dump({"env": settings_env}, f)
    return home


def clean_env(home, **extra):
    env = dict(os.environ)
    for k in ("ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_SUMMARIZER_URL",
              "ROLLING_CONTEXT_SUMMARIZER_KEY", "ROLLING_CONTEXT_SUMMARIZER_FORMAT",
              "ROLLING_CONTEXT_MODEL", "ROLLING_CONTEXT_PORT"):
        env.pop(k, None)
    env.update(HOME=home, USERPROFILE=home)
    env.update(extra)
    return env


# ---------------------------------------------------------------- part A ----

PROBE = (
    "import json, server, compressor;"
    "print(json.dumps({'upstream': server.UPSTREAM_URL,"
    "'summarizer': compressor.SUMMARIZER_BASE_URL,"
    "'native': compressor.NATIVE_MODE,"
    "'fallback': compressor.NATIVE_FALLBACK}))"
)


def resolution_cases(root):
    cases = [
        ("plain Anthropic user is unaffected",
         {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588"}, {},
         {"upstream": ANTHROPIC, "summarizer": ANTHROPIC, "native": True, "fallback": False},
         False),

        ("custom endpoint chained by the hook reaches the summarizer",
         {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
          "ROLLING_CONTEXT_UPSTREAM": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True},
         False),

        ("bare custom ANTHROPIC_BASE_URL is picked up",
         {"ANTHROPIC_BASE_URL": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True},
         False),

        ("env var still wins over settings.json",
         {"ROLLING_CONTEXT_UPSTREAM": "https://from-settings.example"},
         {"ROLLING_CONTEXT_UPSTREAM": "https://from-env.example"},
         {"upstream": "https://from-env.example", "summarizer": "https://from-env.example",
          "native": True, "fallback": True},
         False),

        ("explicit summarizer override still disables native mode",
         {"ROLLING_CONTEXT_UPSTREAM": CUSTOM},
         {"ROLLING_CONTEXT_SUMMARIZER_URL": ANTHROPIC},
         {"upstream": CUSTOM, "summarizer": ANTHROPIC, "native": False, "fallback": False},
         False),

        # Same as case 2, but the file carries a UTF-8 BOM. Before v1.11.3 the
        # reader used encoding="utf-8", which raises on a BOM; the exception was
        # swallowed and the custom endpoint vanished, sending every compaction
        # to api.anthropic.com with the wrong key.
        ("BOM'd settings.json still yields the custom endpoint",
         {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
          "ROLLING_CONTEXT_UPSTREAM": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True},
         True),

        ("BOM'd bare ANTHROPIC_BASE_URL is picked up",
         {"ANTHROPIC_BASE_URL": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True},
         True),
    ]

    failures = 0
    for name, settings, extra, want, bom in cases:
        home = fake_home(os.path.join(root, "res"), settings, bom=bom)
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=PROXY,
                             env=clean_env(home, **extra),
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        got = json.loads(out.stdout.strip().splitlines()[-1])
        bad = {k: (v, got[k]) for k, v in want.items() if got[k] != v}
        print(("  PASS  " if not bad else "  FAIL  ") + name)
        for k, (want_v, got_v) in bad.items():
            print(f"          {k}: want {want_v!r}, got {got_v!r}")
        failures += bool(bad)
        shutil.rmtree(os.path.join(root, "res"), ignore_errors=True)
    return failures


# ---------------------------------------------------------------- part B ----

def conversation(pairs, tail):
    msgs = []
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"question {i} " + "x" * 400})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "y" * 400})
    msgs.append({"role": "user", "content": tail})
    return msgs


def post(port, msgs):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": "glm-4.6", "max_tokens": 64,
                         "stream": True, "messages": msgs}).encode(),
        headers={"content-type": "application/json", "x-api-key": "custom-endpoint-key",
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def live_case(root, strict):
    label = "strict endpoint (rejects cache_control/tool_choice)" if strict else "permissive endpoint"
    work = os.path.join(root, "strict" if strict else "plain")
    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    os.makedirs(work, exist_ok=True)

    home = fake_home(work, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
                            "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mock_port}"})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(proxy_port),
                    ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400")

    mock = subprocess.Popen([sys.executable, MOCK, str(mock_port), log],
                            env=dict(os.environ, MOCK_STRICT="1" if strict else "0"))
    proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(mock_port), "mock endpoint did not start"
        assert wait_port(proxy_port), "proxy did not start"
        base = conversation(24, "turn one: do the thing")
        post(proxy_port, base)
        time.sleep(6)  # background compaction
        turn2 = base + [{"role": "assistant", "content": "did the thing"},
                        {"role": "user", "content": "turn two: keep going"}]
        post(proxy_port, turn2)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    reqs = [json.loads(l)["detail"] for l in open(log, encoding="utf-8")
            if json.loads(l)["kind"] == "request"]
    chat = [r for r in reqs if not r["compaction"]]
    compactions = [r for r in reqs if r["compaction"]]
    local_chars = len(json.dumps(turn2))
    sent = chat[-1]["convo_chars"] if chat else None

    checks = [
        ("compaction reached the custom endpoint", bool(compactions)),
        ("next turn carries the rolling summary", bool(chat) and chat[-1]["carries_summary"]),
        ("next turn sent upstream materially smaller",
         sent is not None and sent < local_chars * 0.75),
    ]
    print(f"  {label}")
    for name, ok in checks:
        print(("    PASS  " if ok else "    FAIL  ") + name)
    if sent is not None:
        print(f"          ({sent:,} chars sent vs {local_chars:,} held locally)")
    return sum(1 for _, ok in checks if not ok)


# ---------------------------------------------------------------- part C ----
# The hook rewrites the user's GLOBAL settings.json on every session start. If
# it cannot parse the file it must leave it alone: regenerating it from {}
# destroys permissions, hooks, enabledPlugins and everything else the user has.
# Losing the proxy chaining is recoverable; losing their config is not.

REAL_CONFIG = {
    "env": {"ANTHROPIC_BASE_URL": CUSTOM},
    "permissions": {"allow": ["Bash(git:*)", "WebSearch"]},
    "hooks": {"SessionStart": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "important-user-hook.ps1"}]}]},
    "enabledPlugins": {"rolling-context@nestor-plugins": True},
    "theme": "dark",
}


def heredoc_python(tmpdir, script_rel, tag):
    """Extract the settings-updating python out of a shipped shell script.

    Deliberately reads the shipped artefact rather than a copy: if the heredoc
    changes, this test changes with it.
    """
    sh = os.path.join(HERE, "..", *script_rel.split("/"))
    with open(sh, encoding="utf-8") as f:
        body = f.read()
    block = body.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    path = os.path.join(tmpdir, f"block_{tag}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(block)
    return path


def write_config(path, mode):
    if mode == "corrupt":
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json at all")
    else:
        with open(path, "w", encoding="utf-8-sig" if mode == "bom" else "utf-8") as f:
            json.dump(REAL_CONFIG, f, indent=2)


def check(failures, label, ok, detail=""):
    print(("    PASS  " if ok else "    FAIL  ") + label)
    if not ok and detail:
        print(f"            {detail}")
    return failures + (not ok)


def chaining_case(root, script_rel, tag, corrupt_exit):
    """start-proxy.sh and install.sh: chain the upstream, never destroy config."""
    print(f"  {script_rel}")
    failures = 0
    for label, mode in (("BOM-less config", "plain"),
                        # PowerShell 5.1 wrote this; git-bash shares $HOME, so
                        # the sh hook read it, called it corrupt, and rewrote
                        # the file from scratch.
                        ("BOM'd config (PowerShell-written)", "bom"),
                        ("truly corrupt config", "corrupt")):
        work = os.path.join(root, "preserve", tag, mode)
        os.makedirs(work, exist_ok=True)
        settings_file = os.path.join(work, "settings.json")
        write_config(settings_file, mode)

        before = open(settings_file, "rb").read()
        proc = subprocess.run(
            [sys.executable, heredoc_python(work, script_rel, tag), settings_file,
             "http://127.0.0.1:5588"], capture_output=True, text=True)
        after_bytes = open(settings_file, "rb").read()

        if mode == "corrupt":
            failures = check(failures, f"{label}: left byte-for-byte untouched",
                             after_bytes == before,
                             f"file was rewritten: {after_bytes[:80]!r}")
            failures = check(failures, f"{label}: exits {corrupt_exit}",
                             proc.returncode == corrupt_exit,
                             f"got {proc.returncode}")
            continue

        after = json.loads(after_bytes.decode("utf-8-sig"))
        lost = [k for k in REAL_CONFIG if k not in after]
        failures = check(failures, f"{label}: user config survives", not lost,
                         f"DESTROYED top-level keys: {lost}")
        chained = after.get("env", {}).get("ROLLING_CONTEXT_UPSTREAM")
        failures = check(failures, f"{label}: custom upstream chained",
                         chained == CUSTOM, f"want {CUSTOM!r}, got {chained!r}")
        failures = check(failures, f"{label}: written without a BOM",
                         not after_bytes.startswith(b"\xef\xbb\xbf"))
    return failures


def uninstall_case(root):
    """uninstall.sh must restore the real endpoint, BOM or not.

    On a BOM'd config this used to parse-fail and exit silently, leaving
    ANTHROPIC_BASE_URL pointed at a proxy that no longer exists — which breaks
    Claude Code outright.
    """
    print("  uninstall.sh")
    failures = 0
    for label, mode in (("BOM-less config", "plain"),
                        ("BOM'd config (PowerShell-written)", "bom")):
        work = os.path.join(root, "preserve", "uninstall", mode)
        os.makedirs(work, exist_ok=True)
        settings_file = os.path.join(work, "settings.json")
        # State the plugin leaves behind while installed.
        cfg = json.loads(json.dumps(REAL_CONFIG))
        cfg["env"] = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
                      "ROLLING_CONTEXT_UPSTREAM": CUSTOM,
                      "ROLLING_CONTEXT_TRIGGER": "100000"}
        with open(settings_file, "w", encoding="utf-8-sig" if mode == "bom" else "utf-8") as f:
            json.dump(cfg, f, indent=2)

        subprocess.run([sys.executable, heredoc_python(work, "uninstall.sh", "uninstall"),
                        settings_file], capture_output=True, text=True)
        after_bytes = open(settings_file, "rb").read()
        after = json.loads(after_bytes.decode("utf-8-sig"))
        env = after.get("env", {})

        failures = check(failures, f"{label}: real endpoint restored",
                         env.get("ANTHROPIC_BASE_URL") == CUSTOM,
                         f"want {CUSTOM!r}, got {env.get('ANTHROPIC_BASE_URL')!r} "
                         f"— Claude Code left pointing at a dead proxy")
        failures = check(failures, f"{label}: plugin vars removed",
                         not [k for k in env if k.startswith("ROLLING_CONTEXT_")],
                         f"left behind: {[k for k in env if k.startswith('ROLLING_CONTEXT_')]}")
        failures = check(failures, f"{label}: user config survives",
                         not [k for k in REAL_CONFIG if k not in after])
    return failures


def preservation_cases(root):
    failures = chaining_case(root, "hooks/start-proxy.sh", "hook", 0)
    failures += chaining_case(root, "install.sh", "install", 1)
    failures += uninstall_case(root)
    return failures


# ---------------------------------------------------------------- part D ----

def script_encoding_cases():
    """Shipped .ps1 files must be ASCII-only or carry a UTF-8 BOM.

    Windows PowerShell 5.1 reads a BOM-less script as ANSI, so a UTF-8 em dash
    decodes to 'a-euro-"' — and that trailing character is U+201D, which
    PowerShell accepts as a string delimiter. One em dash inside a double-quoted
    string is therefore a parse error for the whole file; install.ps1 carried
    exactly that and could not run on Windows at all.

    Note the inversion that makes this easy to get backwards: a BOM on a
    PowerShell *script* is required, while a BOM on the *settings.json* it
    writes is the bug fixed above. Different files, opposite rules.
    """
    failures = 0
    root = os.path.join(HERE, "..")
    scripts = []
    for sub in (".", "hooks", "commands"):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            scripts += [os.path.join(d, n) for n in sorted(os.listdir(d))
                        if n.endswith(".ps1")]
    if not scripts:
        print("    FAIL  no .ps1 files found — test is not looking where it thinks")
        return 1

    for path in scripts:
        raw = open(path, "rb").read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            (raw[3:] if bom else raw).decode("ascii")
            ascii_only = True
        except UnicodeDecodeError:
            ascii_only = False
        name = os.path.relpath(path, root).replace("\\", "/")
        failures = check(failures, f"{name}: ASCII-only or BOM'd", bom or ascii_only,
                         "non-ASCII without a BOM — PS 5.1 will read it as ANSI")
    return failures


def main():
    root = tempfile.mkdtemp(prefix="rolling-context-test-")
    try:
        print("endpoint resolution")
        failures = resolution_cases(root)
        print("\nglobal settings.json preservation")
        failures += preservation_cases(root)
        print("\nshipped script encoding")
        failures += script_encoding_cases()
        print("\nlive proxy against a mock endpoint")
        failures += live_case(root, strict=False)
        failures += live_case(root, strict=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
