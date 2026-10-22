"""`codenotch doctor` — self-diagnosis. Prints lengths and booleans, never a secret, so the output
is safe to paste into an issue."""
from __future__ import annotations

import os
import shutil

from . import __version__, autostart, config, glyphs, hooks
from .providers import antigravity, claude, codex, cursor, glm, grok


def _gi(lines):
    try:
        import gi
    except ImportError:
        lines.append("python3-gi: MISSING — sudo apt install python3-gi")
        return False
    ok = True
    for ns, versions, pkg in (("Gtk", ("3.0",), "gir1.2-gtk-3.0"),
                              ("WebKit2", ("4.1", "4.0"), "gir1.2-webkit2-4.1 (Mint 22) or gir1.2-webkit2-4.0 (Mint 21)"),
                              ("AyatanaAppIndicator3", ("0.1",), "gir1.2-ayatanaappindicator3-0.1 (tray icon, optional)")):
        found = None
        for v in versions:
            try:
                gi.require_version(ns, v)
                __import__("gi.repository." + ns)
                found = v
                break
            except (ValueError, ImportError):
                continue
        if found:
            lines.append(f"{ns} {found}: ok")
        else:
            lines.append(f"{ns}: MISSING — install {pkg}")
            if ns != "AyatanaAppIndicator3":
                ok = False
    try:
        import cairo  # noqa: F401
        lines.append("pycairo: ok")
    except ImportError:
        lines.append("pycairo: MISSING — sudo apt install python3-gi-cairo")
        ok = False
    return ok


def _display(lines):
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    lines.append(f"session: {session} (DISPLAY={os.environ.get('DISPLAY', '')!r})")
    if session == "wayland":
        lines.append("  note: the notch needs X11 placement; it runs through XWayland, which may not keep it on top")
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        os.environ.setdefault("GDK_BACKEND", "x11")
        from gi.repository import Gdk
        disp = Gdk.Display.get_default()
        if disp is None:
            lines.append("display: none (run doctor inside the desktop session)")
            return
        scr = disp.get_default_screen()
        lines.append(f"compositor: {'on' if scr.is_composited() else 'OFF — enable it in Window Manager Tweaks → Compositor'}")
        for i in range(disp.get_n_monitors()):
            m = disp.get_monitor(i)
            g, w = m.get_geometry(), m.get_workarea()
            lines.append(f"monitor {i + 1}: {m.get_model()} {g.width}x{g.height}+{g.x}+{g.y} "
                         f"work={w.width}x{w.height}+{w.x}+{w.y} scale={m.get_scale_factor()}"
                         f"{' primary' if m.is_primary() else ''}")
    except (ImportError, ValueError) as e:
        lines.append(f"display: unavailable ({e})")


def run() -> int:
    lines = [f"Codenotch for Linux {__version__} — doctor", f"config dir: {config.config_dir()}"]
    ok = _gi(lines)
    _display(lines)
    lines.append("")
    for p in claude.profiles():
        cred = claude._read_credentials(p.dir)
        if cred:
            lines.append(f"Claude[{p.account_name()}]: credential found (token {len(cred.token)} chars, "
                         f"{'expired' if cred.expired(config.now_ms()) else 'valid'}, plan {cred.plan or '?'})")
        else:
            lines.append(f"Claude[{p.account_name()}]: {p.dir / '.credentials.json'} not found (sign in with the claude CLI)")
    cli = claude.find_cli()
    lines.append(f"Claude CLI: {cli if cli else 'not found (token renewal and Sign in need it)'}")
    c = codex._load_credential()
    lines.append(f"Codex: {'signed in' if c else 'no auth.json'}; rollout: {codex._newest_rollout() or 'none'}")
    lines.append(f"Cursor: {'state.vscdb found' if cursor.present() else 'not installed'}"
                 + (f", session {'borrowed' if cursor._read_credentials() else 'not readable'}" if cursor.present() else ""))
    lines.append(f"Grok: {'auth.json found' if grok.present() else 'not installed'}"
                 + (f", xAI session {'found' if grok._read_credentials() else 'missing'}" if grok.present() else ""))
    lines.append(f"Antigravity: agy CLI {antigravity.find_agy() or 'not found'}; state roots {len(antigravity.state_roots())}")
    g = glm._load_credential()
    lines.append(f"z.ai (GLM): {'key from ' + g.source if g else ('no usable key' if glm.present() else 'not configured')}")
    lines.append("")
    lines.append(f"hooks installed: {hooks.is_installed()} (hook binary: {hooks.hook_executable() or 'missing'})")
    lines.append(f"open at login: {autostart.is_enabled()}")
    lines.append(f"glyphs: {', '.join(sorted(glyphs.collect()))}")
    lines.append(f"codenotch on PATH: {shutil.which('codenotch') or 'no'}")
    out = "\n".join(lines)
    print(out)
    try:
        (config.config_dir() / "doctor.log").write_text(out + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1
