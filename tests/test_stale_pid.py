#!/usr/bin/env python3
"""Regression suite for issue #9 — a dead proxy that never gets restarted.

The start hook used to gate on the PID file: does it exist, and is that PID
alive. Both can be true with nothing serving. The reporter's proxy died, its
PID file stayed on disk, the number was handed to some other process, and
every session after that logged "Proxy already running" while failing with
ConnectionRefused — until a human deleted the file by hand.

So these cases drive the real hook against a throwaway HOME and assert on what
ends up listening on the port, never on what the PID file says:

  A. dead PID in the PID file      -> proxy is started, port answers
  B. RECYCLED PID (the bug)        -> proxy is started, and the innocent
                                      process wearing that number survives
  C. healthy proxy, same version   -> left alone, same process still serving
  D. healthy proxy, older version  -> replaced, new version answers
  E. port held by a stranger       -> refuses to start, says so, and does not
                                      kill the stranger

  python tests/test_stale_pid.py
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Forward slashes throughout: these paths are handed to bash (git-bash on
# Windows), where a backslashed path makes `dirname` return "." and the hook
# would resolve its own directory to the cwd. Python accepts them everywhere.
HOOK = os.path.join(ROOT, "hooks", "start-proxy.sh").replace("\\", "/")
MANIFEST = os.path.join(ROOT, ".claude-plugin", "plugin.json")


def find_bash():
    """A bash that can run the hook.

    On Windows `bash` on PATH is usually WSL's, which lives in a different
    filesystem and cannot see E:/Repos/... — it reports "No such file or
    directory" for a script that is plainly there. Git for Windows' bash is
    the one that matches how Claude Code runs this hook.
    """
    if os.name != "nt":
        return shutil.which("bash")
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = find_bash()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def plugin_version():
    with open(MANIFEST, encoding="utf-8-sig") as f:
        return json.load(f)["version"]


def health(port, timeout=0.5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def wait_health(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        h = health(port)
        if h:
            return h
        time.sleep(0.2)
    return None


def alive(proc):
    return proc.poll() is None


def died(proc, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    return False


def kill_pid(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        pass


def fake_home(root, name):
    home = f"{root}/{name}"
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    # An upstream that exists but is never reached: these cases only ever hit
    # /health, and a real one would be a live credential in a test.
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:9"}}, f)
    return home


def run_hook(home, port):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("ROLLING_CONTEXT_"):
            env.pop(k)
    env.update(HOME=home, USERPROFILE=home, ROLLING_CONTEXT_PORT=str(port))
    return subprocess.run([BASH, HOOK], capture_output=True, text=True,
                          env=env, timeout=180)


def hook_log(home):
    path = os.path.join(home, ".claude", "rolling-context-hook.log")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def pidfile(home):
    path = os.path.join(home, ".claude", "rolling-context-proxy.pid")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_pidfile(home, value, version=None):
    with open(os.path.join(home, ".claude", "rolling-context-proxy.pid"), "w") as f:
        f.write(str(value))
    if version is not None:
        with open(os.path.join(home, ".claude", "rolling-context-proxy.version"), "w") as f:
            f.write(version)


def start_proxy(home, port, plugin_root=ROOT):
    """Start the proxy the way the hook does, and wait for it to serve."""
    env = dict(os.environ)
    env.update(HOME=home, USERPROFILE=home, ROLLING_CONTEXT_PORT=str(port))
    log = open(os.path.join(home, ".claude", "manual-proxy.log"), "ab")
    proc = subprocess.Popen([sys.executable, "server.py"],
                            cwd=os.path.join(plugin_root, "proxy"),
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    if not wait_health(port):
        proc.kill()
        raise RuntimeError("test setup: proxy never came up")
    return proc


def old_version_checkout(root, version="0.0.1-old"):
    """A copy of the plugin stamped with an older version.

    Nothing is faked: the copy's proxy reads its own manifest and honestly
    reports the old version on /health, which is what an upgrade actually
    looks like from the hook's side.
    """
    dst = os.path.join(root, "old-checkout")
    if not os.path.exists(dst):
        os.makedirs(dst)
        for sub in ("proxy", ".claude-plugin"):
            shutil.copytree(os.path.join(ROOT, sub), os.path.join(dst, sub),
                            ignore=shutil.ignore_patterns("venv", "__pycache__"))
        manifest = os.path.join(dst, ".claude-plugin", "plugin.json")
        with open(manifest, encoding="utf-8-sig") as f:
            data = json.load(f)
        data["version"] = version
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return dst


def dead_pid():
    """A PID that is certainly not in use: spawn, kill, reap."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def check(results, name, ok, note=None):
    results.append(ok)
    print(("    PASS  " if ok else "    FAIL  ") + name)
    if note:
        print(f"          {note}")


def kill(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------- part A ----

def stale_dead_pid(root):
    print("  stale PID file, recorded process is gone")
    home, port, results = fake_home(root, "dead"), free_port(), []
    write_pidfile(home, dead_pid(), plugin_version())

    run_hook(home, port)
    h = health(port)
    check(results, "proxy is running despite the leftover PID file", bool(h),
          None if h else (hook_log(home).strip().splitlines() or [""])[-1])
    check(results, "PID file now records the live proxy",
          bool(h) and pidfile(home) == str(h.get("pid")))
    if h:
        kill_pid(h["pid"])
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part B ----

def recycled_pid(root):
    """The reported bug. A live process wearing the dead proxy's number."""
    print("  recycled PID: the number is alive, the proxy is not")
    home, port, results = fake_home(root, "recycled"), free_port(), []

    innocent = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(120)"])
    try:
        write_pidfile(home, innocent.pid, plugin_version())
        run_hook(home, port)

        h = health(port)
        # Before the fix this is exactly where it failed: `kill -0` said the
        # PID was alive, the hook logged "Proxy already running", and nothing
        # was listening.
        check(results, "proxy is started even though the recorded PID is alive",
              bool(h))
        check(results, "hook does not claim the proxy was already running",
              "already running" not in hook_log(home))
        check(results, "the unrelated process holding that PID is NOT killed",
              alive(innocent))
        check(results, "hook says the PID was recycled rather than killing it",
              "recycled PID" in hook_log(home))
        check(results, "PID file no longer points at the innocent process",
              pidfile(home) != str(innocent.pid))
        if h:
            kill_pid(h["pid"])
    finally:
        kill(innocent)
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part C ----

def healthy_is_left_alone(root):
    print("  healthy proxy of the current version")
    home, port, results = fake_home(root, "healthy"), free_port(), []
    proc = start_proxy(home, port)
    try:
        write_pidfile(home, proc.pid, plugin_version())
        before = health(port)
        run_hook(home, port)
        after = health(port)

        check(results, "the running proxy is not restarted",
              bool(after) and after.get("pid") == before.get("pid"))
        check(results, "the process is still alive", alive(proc))
        check(results, "hook reports it as already healthy",
              "healthy" in hook_log(home).lower())
    finally:
        kill(proc)
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part D ----

def version_change_restarts(root):
    """An upgrade must still replace a running proxy — including one old
    enough that its PID file was never written by this version of the hook."""
    print("  running proxy is an older version")
    home, port, results = fake_home(root, "upgrade"), free_port(), []
    proc = start_proxy(home, port, plugin_root=old_version_checkout(root))
    try:
        # No PID file at all: the only way to find the old process is the PID
        # /health reports.
        write_pidfile(home, "", "0.0.1-old")
        old = health(port)
        check(results, "the running proxy really is the old version",
              bool(old) and old.get("version") == "0.0.1-old")

        run_hook(home, port)
        check(results, "the old process was stopped", died(proc))
        new = wait_health(port)
        check(results, "a proxy is serving after the upgrade", bool(new))
        check(results, "it is a different process",
              bool(new) and new.get("pid") != old.get("pid"))
        check(results, "it reports the current version",
              bool(new) and new.get("version") == plugin_version())
        if new:
            kill_pid(new["pid"])
    finally:
        kill(proc)
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part E ----

def foreign_port_holder(root):
    print("  port held by an unrelated server")
    home, port, results = fake_home(root, "foreign"), free_port(), []
    squatter = subprocess.Popen(
        [sys.executable, "-c",
         "import http.server, sys;"
         "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])),"
         " http.server.SimpleHTTPRequestHandler).serve_forever()",
         str(port)],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.time() + 10
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                time.sleep(0.15)
        run_hook(home, port)
        log = hook_log(home)

        check(results, "hook refuses to start on an occupied port",
              "is held by something that is not this proxy" in log)
        check(results, "the unrelated server is left running", alive(squatter))
        check(results, "no PID file is written", pidfile(home) == "")
    finally:
        kill(squatter)
    return sum(1 for ok in results if not ok)


def main():
    if not os.path.exists(HOOK):
        print(f"missing {HOOK}")
        sys.exit(1)
    if not BASH:
        print("SKIPPED: no POSIX bash found to run the hook with "
              "(on Windows, install Git for Windows — WSL's bash cannot see "
              "the repo path)")
        sys.exit(0)
    root = tempfile.mkdtemp(prefix="rolling-context-stalepid-").replace("\\", "/")
    try:
        print("stale pid / liveness (issue #9)")
        failures = stale_dead_pid(root)
        print()
        failures += recycled_pid(root)
        print()
        failures += healthy_is_left_alone(root)
        print()
        failures += version_change_restarts(root)
        print()
        failures += foreign_port_holder(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
