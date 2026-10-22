"""User-initiated Claude sign-in through the standalone Claude Code CLI, in a terminal window
(ported from claude_auth.rs). OAuth stays in the CLI: no code, token or URL crosses the bridge.
The CLI path reaches the shell through the environment, never through the script text."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .providers import claude

TIMEOUT_SECS = 15 * 60
_SCRIPT = ('printf "%s\\n" "Complete sign-in in your browser. Paste any code in this window."; '
           '"$CODENOTCH_CLAUDE_CLI" auth login --claudeai; r=$?; '
           'if [ $r -eq 0 ]; then echo "Sign-in complete. Codenotch will refresh automatically."; sleep 2; '
           'else echo "Sign-in failed or cancelled. Retry from Codenotch."; sleep 8; fi; exit $r')

_lock = threading.Lock()
_busy = False
_message = ""


def state() -> dict:
    return {"busy": _busy, "message": _message}


def busy() -> bool:
    return _busy


def usage_succeeded() -> None:
    global _message
    if not _busy:
        _message = ""


def _terminal_command():
    """A terminal that stays attached until the shell exits, so the sign-in can be awaited."""
    title = "Codenotch - Claude sign-in"
    if shutil.which("xfce4-terminal"):
        return ["xfce4-terminal", "--disable-server", f"--title={title}", "-x", "sh", "-c", _SCRIPT]
    if shutil.which("gnome-terminal"):
        return ["gnome-terminal", "--wait", f"--title={title}", "--", "sh", "-c", _SCRIPT]
    if shutil.which("mate-terminal"):
        return ["mate-terminal", "--disable-factory", f"--title={title}", "-x", "sh", "-c", _SCRIPT]
    if shutil.which("konsole"):
        return ["konsole", "--nofork", "-e", "sh", "-c", _SCRIPT]
    if shutil.which("xterm"):
        return ["xterm", "-T", title, "-e", "sh", "-c", _SCRIPT]
    if shutil.which("x-terminal-emulator"):
        return ["x-terminal-emulator", "-e", "sh", "-c", _SCRIPT]
    return None


def start_login(on_done) -> None:
    global _busy, _message
    cli = claude.find_cli()
    if cli is None:
        raise RuntimeError("Claude Code CLI not found. Install the standalone CLI first.")
    cmd = _terminal_command()
    if cmd is None:
        raise RuntimeError("Unable to open Claude sign-in window.")
    with _lock:
        if _busy:
            raise RuntimeError("Claude sign-in or renewal is already running.")
        _busy = True
    env = {k: v for k, v in os.environ.items()
           if not (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_")
                   or k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))}
    env["CODENOTCH_CLAUDE_CLI"] = str(cli)
    try:
        child = subprocess.Popen(cmd, env=env, cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        _busy = False
        raise RuntimeError("Unable to open Claude sign-in window.")
    _message = "Complete sign-in in the browser or terminal window."

    def wait():
        global _busy, _message
        ok = False
        deadline = time.monotonic() + TIMEOUT_SECS
        while time.monotonic() < deadline:
            rc = child.poll()
            if rc is not None:
                ok = rc == 0
                break
            time.sleep(0.25)
        else:
            child.kill()
            child.wait()
        _message = ("Sign-in complete. Refreshing usage..." if ok
                    else "Sign-in cancelled, failed or timed out. Try again.")
        _busy = False
        on_done()

    threading.Thread(target=wait, name="claude-sign-in", daemon=True).start()
