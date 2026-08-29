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
import re
import shutil
import socket
import subprocess
import sys
import threading
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
    # Strip every ROLLING_CONTEXT_* var, not a hand-listed subset: a knob added
    # later would otherwise leak the developer's own shell into the test.
    for k in [k for k in env if k.startswith("ROLLING_CONTEXT_")]:
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


def post(port, msgs, sid=""):
    headers = {"content-type": "application/json", "x-api-key": "custom-endpoint-key",
               "anthropic-version": "2023-06-01"}
    if sid:
        headers["X-Claude-Code-Session-Id"] = sid
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": "glm-4.6", "max_tokens": 64,
                         "stream": True, "messages": msgs}).encode(),
        headers=headers,
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


LOCAL_ENDPOINTS = [
    # (label, existing ANTHROPIC_BASE_URL, must the hook chain it?)
    ("llama.cpp on 18080",   "http://127.0.0.1:18080", True),
    ("Ollama on 11434",      "http://127.0.0.1:11434", True),
    ("LM Studio on 1234",    "http://localhost:1234",  True),
    ("vLLM on 8000",         "http://127.0.0.1:8000",  True),
    ("a remote endpoint",    CUSTOM,                   True),
    # …but our own proxy must NOT be chained to itself, under either name.
    ("the proxy itself",     "http://127.0.0.1:5588",  False),
    ("the proxy as localhost", "http://localhost:5588", False),
]


def local_endpoint_case(root, script_rel, tag):
    """A local model endpoint must be chained, not mistaken for the proxy.

    The sh hook tested `"127.0.0.1" not in existing`, with no port check, so
    ANY loopback endpoint looked like the proxy was already installed and
    chaining was skipped — the plugin then sat inert for exactly the users the
    README tells to run local models (Ollama, llama.cpp, LM Studio, vLLM).
    Found by running Claude Code against a real llama.cpp server in a container:
    the proxy was up, and not one request went through it.
    """
    print(f"  {script_rel}: local endpoints")
    failures = 0
    for label, url, should_chain in LOCAL_ENDPOINTS:
        work = os.path.join(root, "local", tag, re.sub(r"\W+", "_", label))
        os.makedirs(work, exist_ok=True)
        settings_file = os.path.join(work, "settings.json")
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": url}}, f, indent=2)

        subprocess.run([sys.executable, heredoc_python(work, script_rel, tag),
                        settings_file, "http://127.0.0.1:5588"],
                       capture_output=True, text=True)
        env = json.load(open(settings_file, encoding="utf-8-sig")).get("env", {})
        upstream = env.get("ROLLING_CONTEXT_UPSTREAM")
        base = env.get("ANTHROPIC_BASE_URL")

        if should_chain:
            ok = upstream == url and base == "http://127.0.0.1:5588"
            detail = (f"upstream={upstream!r} base={base!r} — traffic bypasses "
                      f"the proxy entirely")
        else:
            ok = upstream is None and base == url
            detail = f"upstream={upstream!r} — the proxy was chained to itself"
        failures = check(failures, f"{label}: {'chained' if should_chain else 'left alone'}",
                         ok, detail)
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
    # The settings.json bookkeeping the hooks/install/uninstall scripts used to
    # inline (ANTHROPIC_BASE_URL chaining) now lives in proxy/wire.py, which the
    # scripts call. Its chaining/gateway/unwire behaviour — including "never
    # destroy the user's config" — is covered by tests/test_wire.py. Run it here
    # so this suite still gates that logic.
    here = os.path.dirname(os.path.abspath(__file__))
    rc = subprocess.run([sys.executable, os.path.join(here, "test_wire.py")],
                        capture_output=True, text=True)
    sys.stdout.write(rc.stdout)
    if rc.returncode != 0:
        sys.stdout.write(rc.stderr)
    return 1 if rc.returncode != 0 else 0


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


# ---------------------------------------------------------------- part E ----
# Issue #7: the proxy logged at DEBUG unconditionally into an unrotated file,
# while the start hook redirected stdout into a second one. Every line hit disk
# twice and nothing ever pruned either — 5.9 GB of rolling-context-proxy.log on
# a proxy left up for days.

LOG_PROBE = (
    "import json, logging, server;"
    "hs=logging.getLogger().handlers;"
    "r=[x for x in hs if hasattr(x,'maxBytes')];"
    "s=[x for x in hs if not hasattr(x,'maxBytes')];"
    "print('PROBE'+json.dumps({'rotating': bool(r),"
    "'file_level': logging.getLevelName(r[0].level) if r else None,"
    "'max_bytes': r[0].maxBytes if r else 0,"
    "'backups': r[0].backupCount if r else 0,"
    "'stream_level': logging.getLevelName(s[0].level) if s else None}))"
)

ROTATE_PROBE = (
    "import os, server;"
    "[server.log.info('x'*200) for _ in range(40000)];"
    "d=os.path.join(os.path.expanduser('~'), '.claude');"
    "print('PROBE'+str(sum(os.path.getsize(os.path.join(d,f))"
    " for f in os.listdir(d) if 'debug.log' in f)))"
)


def probe(root, tag, code, **env):
    home = fake_home(os.path.join(root, tag), {})
    out = subprocess.run([sys.executable, "-c", code], cwd=PROXY,
                         env=clean_env(home, **env), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for line in out.stdout.splitlines():
        if line.startswith("PROBE"):
            return line[5:]
    raise AssertionError(f"probe produced no output: {out.stdout[:300]} {out.stderr[:300]}")


def logging_cases(root):
    failures = 0

    cfg = json.loads(probe(root, "log-default", LOG_PROBE))
    failures = check(failures, "debug log rotates", cfg["rotating"],
                     "no RotatingFileHandler — the log grows without bound")
    failures = check(failures, "defaults to INFO, not DEBUG",
                     cfg["file_level"] == "INFO", f"got {cfg['file_level']}")
    failures = check(failures, "rotation bound is sane",
                     cfg["max_bytes"] > 0 and cfg["backups"] > 0,
                     f"maxBytes={cfg['max_bytes']} backups={cfg['backups']}")
    # stdout is redirected into rolling-context-proxy.log, which the start hook
    # only truncates on restart. Routine traffic must not go there.
    failures = check(failures, "stdout carries WARNING and above only",
                     cfg["stream_level"] == "WARNING", f"got {cfg['stream_level']}")

    dbg = json.loads(probe(root, "log-debug", LOG_PROBE,
                           ROLLING_CONTEXT_LOG_LEVEL="DEBUG"))
    failures = check(failures, "ROLLING_CONTEXT_LOG_LEVEL raises the level",
                     dbg["file_level"] == "DEBUG", f"got {dbg['file_level']}")

    # The claim that matters: write far past the cap, disk stays bounded.
    total = int(probe(root, "log-rotate", ROTATE_PROBE,
                      ROLLING_CONTEXT_LOG_MAX_MB="1", ROLLING_CONTEXT_LOG_BACKUPS="2"))
    cap = 3 * 1024 * 1024 + 65536  # (1 active + 2 backups) x 1 MB, plus slack
    failures = check(failures, "8 MB of log lines stay bounded on disk", total <= cap,
                     f"{total:,} bytes on disk, expected <= {cap:,}")
    print(f"            ({total:,} bytes on disk from ~8,000,000 bytes written)")
    return failures


# ---------------------------------------------------------------- part F ----
# Issue #8: the compaction guard was process-global. ANY conversation compacting
# silently blocked EVERY other conversation from starting one — via a bare
# `pass`, so nothing was logged and it never showed up in anyone's logs. Most
# conversations therefore never got a compression entry at all, and so had
# nothing to inject on later turns. The reporter measured injection on 633 of
# 2,962 eligible requests (21.4%) over 16.5 hours.
#
# The trap this test exists to avoid: the mock must be threaded and must report
# token counts from the actual request. A single-threaded mock serialises the
# proxy and hides the bug (it produced a false negative during diagnosis), and a
# constant token count makes every conversation demand compaction forever, which
# is not a real workload.

def distinct_conversation(tag, filler, pairs=24):
    """A conversation that shares NO message with any other tag.

    conversation() above builds the same 24 pairs every time and varies only the
    last message, so one conversation's summary matches another's prefix — which
    made an earlier version of this test pass against the unfixed server for
    entirely the wrong reason.
    """
    msgs = []
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"{tag} question {i} " + filler * 400})
        msgs.append({"role": "assistant", "content": f"{tag} answer {i} " + filler * 400})
    msgs.append({"role": "user", "content": f"{tag}: do the thing"})
    return msgs


def concurrency_case(root):
    print("  two conversations, one compacting")
    failures = 0
    mock_port, proxy_port = free_port(), free_port()
    work = os.path.join(root, "concurrency")
    os.makedirs(work, exist_ok=True)
    log = os.path.join(work, "mock.jsonl")

    home = fake_home(work, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
                            "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mock_port}"})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(proxy_port),
                    ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400")
    mock = subprocess.Popen(
        [sys.executable, MOCK, str(mock_port), log],
        env=dict(os.environ, MOCK_COMPACTION_DELAY="4", MOCK_TOKENS_FROM_SIZE="1"))
    proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(mock_port), "mock endpoint did not start"
        assert wait_port(proxy_port), "proxy did not start"

        alpha = distinct_conversation("ALPHA", "a")
        bravo = distinct_conversation("BRAVO", "b")

        post(proxy_port, alpha, sid="session-alpha")
        time.sleep(1.0)                     # ALPHA's compaction is still running
        post(proxy_port, bravo, sid="session-bravo")
        time.sleep(8)                       # let both compactions finish

        # Second turn each: both should now carry a summary.
        a2 = alpha + [{"role": "assistant", "content": "did it"},
                      {"role": "user", "content": "ALPHA: keep going"}]
        b2 = bravo + [{"role": "assistant", "content": "did it"},
                      {"role": "user", "content": "BRAVO: keep going"}]
        post(proxy_port, a2, sid="session-alpha")
        post(proxy_port, b2, sid="session-bravo")
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    reqs = [json.loads(l)["detail"] for l in open(log, encoding="utf-8")
            if json.loads(l)["kind"] == "request"]
    chats = [r for r in reqs if not r["compaction"]]

    # Count compactions that started BEFORE the second turns were sent. Counting
    # all of them is too weak: the blocked conversation does eventually compact
    # once the other finishes, just far too late to help the turn that needed it.
    second_turn_start = len(reqs) - 1
    seen_chats = 0
    for i in range(len(reqs) - 1, -1, -1):
        if not reqs[i]["compaction"]:
            seen_chats += 1
            if seen_chats == 2:
                second_turn_start = i
                break
    early = [r for r in reqs[:second_turn_start] if r["compaction"]]

    failures = check(failures, "both conversations compacted concurrently",
                     len(early) >= 2,
                     f"only {len(early)} compaction(s) started while the other was "
                     f"running — one conversation was blocked by the other")
    # The last two chats are the second turns; both must carry the summary.
    tail = chats[-2:]
    failures = check(failures, "both second turns carry a summary",
                     len(tail) == 2 and all(c["carries_summary"] for c in tail),
                     f"carries_summary={[c['carries_summary'] for c in tail]}")
    return failures


def subagent_case(root):
    """A parent and its subagents share ONE session id (server.py:204).

    Subagents get their own transcript, not their own session. Keying
    concurrency on the session id alone therefore collapses an entire agent team
    into a single conversation and they block each other — #8 again, scoped to
    the team. Measured before the fix: 2 compactions instead of 3, and neither
    subagent ever received a summary.
    """
    print("  one session id, parent + 2 subagents")
    failures = 0
    mock_port, proxy_port = free_port(), free_port()
    work = os.path.join(root, "subagents")
    os.makedirs(work, exist_ok=True)
    log = os.path.join(work, "mock.jsonl")

    home = fake_home(work, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
                            "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mock_port}"})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(proxy_port),
                    ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400")
    mock = subprocess.Popen(
        [sys.executable, MOCK, str(mock_port), log],
        env=dict(os.environ, MOCK_COMPACTION_DELAY="4", MOCK_TOKENS_FROM_SIZE="1"))
    proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SID = "shared-session-id-parent-and-subagents"
    try:
        assert wait_port(mock_port), "mock endpoint did not start"
        assert wait_port(proxy_port), "proxy did not start"

        convos = {
            "PARENT": distinct_conversation("PARENT", "p"),
            "SUB1": distinct_conversation("SUBAGENT-ONE", "q"),
            "SUB2": distinct_conversation("SUBAGENT-TWO", "r"),
        }
        post(proxy_port, convos["PARENT"], sid=SID)
        time.sleep(0.8)                      # parent's compaction is running
        threads = [threading.Thread(target=post, args=(proxy_port, convos[t], SID))
                   for t in ("SUB1", "SUB2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(9)                        # let all three finish

        for tag, base in convos.items():
            post(proxy_port, base + [{"role": "assistant", "content": "ok"},
                                     {"role": "user", "content": f"{tag} next"}],
                 sid=SID)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    reqs = [json.loads(l)["detail"] for l in open(log, encoding="utf-8")
            if json.loads(l)["kind"] == "request"]
    compactions = [r for r in reqs if r["compaction"]]
    tail = [r for r in reqs if not r["compaction"]][-3:]

    failures = check(failures, "all three transcripts compacted",
                     len(compactions) >= 3,
                     f"only {len(compactions)} — the team shares one session id "
                     f"and blocked itself")
    failures = check(failures, "parent and both subagents carry a summary",
                     len(tail) == 3 and all(c["carries_summary"] for c in tail),
                     f"carries_summary={[c['carries_summary'] for c in tail]}")
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
        print("\nlog volume and rotation")
        failures += logging_cases(root)
        print("\nconcurrent conversations")
        failures += concurrency_case(root)
        failures += subagent_case(root)
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
