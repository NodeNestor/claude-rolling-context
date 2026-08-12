#!/usr/bin/env python3
"""Runtime on/off switch for rolling-context.

Two scopes:

  session (default)  Turn compression off for THIS conversation only, leaving
                     every other session alone. Issue #6: heavy planning and
                     optimization work wants the whole repo in view, but the
                     other windows open on the same machine still want the
                     savings.

  --global           Turn it off for every session on this machine.

`ROLLING_CONTEXT_DISABLE=1` already worked for the global case, but it lives in
settings.json env and needs both a Claude Code restart and a proxy restart to
take effect — useless at the moment you actually want it, which is mid-session.
The global scope is a flag file the proxy checks on every request instead, so
it applies to the very next request.

The session scope carries no state at all. This command prints a marker; slash
command output is inserted into the transcript inside <local-command-stdout>,
so from then on every request in this conversation carries it, and the proxy
reads the newest marker to decide. That keeps the proxy's founding property
intact — no sessions, no fingerprints, just content recognition — and means a
session that is closed and resumed keeps whatever it was set to, for free.

Scanning is confined to <local-command-stdout> blocks precisely so that reading
this file in a conversation does not toggle anything: a Read tool result is not
wrapped in that tag. `status` deliberately never prints the marker literals,
for the same reason.

Turning off is NOT destructive in either scope. Stored compressions stay in the
proxy's memory untouched; they simply stop being injected, and no new
compression is triggered. Turning back on resumes with everything already
computed, so a session that ran hot before the toggle never pays to compress
that history twice.

The global flag lives at a fixed user-level path rather than in the plugin data
dir: slash commands run through Bash, which does not inherit CLAUDE_PLUGIN_DATA,
so a data-dir flag would be written somewhere the proxy never looks. A fixed
path also survives version bumps, which move the plugin cache directory.

  python switch.py on|off [--global] | status
"""
import json
import os
import sys

# Kept in one place; server.py imports these rather than restating them.
MARKER_OFF = "<<rolling-context:session-off>>"
MARKER_ON = "<<rolling-context:session-on>>"


def flag_dir():
    return os.environ.get("ROLLING_CONTEXT_HOME") or os.path.join(
        os.path.expanduser("~"), ".claude-rolling-context"
    )


def flag_path():
    return os.path.join(flag_dir(), "disabled")


def config_path():
    return os.path.join(flag_dir(), "config.json")


def proxy_line():
    """One line on whether the proxy is actually up.

    "rolling-context: ON" used to be printed by a status command that had
    never checked whether anything was listening — the same blind spot that
    let a crashed proxy read as running (issue #9). On is only meaningful if
    there is a proxy to be on.
    """
    import urllib.request

    port = os.environ.get("ROLLING_CONTEXT_PORT") or "5588"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        # ASCII only: this prints to a terminal that may be cp1252, where an
        # em dash lands as a replacement character.
        return (f"proxy: NOT RUNNING on port {port} - sessions will fail with "
                f"connection refused.\n"
                f"  It restarts on the next Claude Code session; see "
                f"~/.claude/rolling-context-hook.log")
    if body.get("service") != "rolling-context":
        return f"proxy: port {port} is answering, but it is not this proxy"
    return (f"proxy: running on port {port} (v{body.get('version', '?')}, "
            f"{body.get('compression_count', 0)} compressions, "
            f"{body.get('total_tokens_saved', 0):,} tokens saved)")


def config_default_enabled():
    """The default for sessions that have not toggled themselves.

    A fresh install compresses out of the box — that is the whole point of
    installing it — so the built-in default is on, and {"enabled": false} in
    config.json flips it for people who would rather opt in per session.

    Deliberately forgiving: a missing, unreadable, or malformed config falls
    back to on rather than silently disabling the thing the user installed.
    """
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return True
    value = cfg.get("enabled", True) if isinstance(cfg, dict) else True
    return value if isinstance(value, bool) else True


def env_disabled():
    return os.environ.get("ROLLING_CONTEXT_DISABLE") == "1"


def is_disabled():
    """True when the proxy should pass every session through untouched.

    Read fresh on every call — the whole point is that the flag takes effect
    without restarting the proxy, so this must never be cached.
    """
    if env_disabled():
        return True
    try:
        return os.path.exists(flag_path())
    except OSError:
        return False


def _set(disabled):
    path = flag_path()
    if disabled:
        os.makedirs(flag_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("rolling-context disabled via /rolling-context:off --global\n")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _session(marker, lines):
    print(marker)
    for line in lines:
        print(line)


def main():
    argv = [a.lower() for a in sys.argv[1:]]
    is_global = bool({"--global", "-g"} & set(argv))
    action = next((a for a in argv if not a.startswith("-")), "status")

    if action == "off":
        if is_global:
            _set(True)
            print("rolling-context is now OFF on this machine — every session sends")
            print("its full history upstream, uncompressed.")
            print("Applies from the next request; no restart needed.")
            print("Already-computed compressions are kept, not discarded.")
            print("Turn it back on with /rolling-context:on --global")
            return
        _session(MARKER_OFF, [
            "rolling-context is now OFF for this conversation — it will send its",
            "full history upstream, uncompressed. Other sessions are unaffected.",
            "Applies from the next request; no restart needed.",
            "Already-computed compressions are kept, not discarded.",
            "Turn it back on with /rolling-context:on",
        ])
        return

    if action == "on":
        if is_global:
            _set(False)
            if env_disabled():
                print("Flag cleared, but ROLLING_CONTEXT_DISABLE=1 is set in the")
                print("environment and still wins. Remove it from settings.json")
                print("and restart Claude Code.")
                return
            print("rolling-context is now ON for this machine — compression active")
            print("from the next request.")
            print("Compressions computed before the toggle are reused as-is.")
            return
        notes = [
            "rolling-context is now ON for this conversation — compression active",
            "from the next request.",
            "Compressions computed before the toggle are reused as-is.",
        ]
        if is_disabled():
            notes += [
                "",
                "Note: it is still off machine-wide, which wins over this session.",
                "Clear that with /rolling-context:on --global",
            ]
        _session(MARKER_ON, notes)
        return

    if action == "status":
        # Never print the marker literals here — this output lands in the
        # transcript inside <local-command-stdout>, which is exactly what the
        # proxy scans, so a chatty status would toggle the session it reports on.
        print(proxy_line())
        if env_disabled():
            print("rolling-context: OFF machine-wide (ROLLING_CONTEXT_DISABLE=1 in the environment)")
        elif os.path.exists(flag_path()):
            print("rolling-context: OFF machine-wide (switched off with /rolling-context:off --global)")
            print(f"  flag: {flag_path()}")
        else:
            print("rolling-context: ON machine-wide")
        default_on = config_default_enabled()
        print(f"  default for new sessions: {'ON' if default_on else 'OFF'}"
              + ("" if os.path.exists(config_path()) else "  (built-in)"))
        print(f"  config: {config_path()}")
        print("")
        print("Per-session state is carried in the conversation itself, so it is not")
        print("readable from here. Whichever of /rolling-context:off or")
        print("/rolling-context:on you ran last in this session is the one in effect;")
        print("if you have run neither, this session follows the machine-wide state.")
        return

    print(f"Unknown action {action!r} — expected on, off, or status")
    sys.exit(2)


if __name__ == "__main__":
    main()
