"""Open at login, through the XDG autostart spec every Mint XFCE release honours: a .desktop file
in ~/.config/autostart."""
from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

_NAME = "codenotch.desktop"

_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Codenotch
Comment=AI usage notch for the screen edge
Exec={exec_cmd}
Icon=codenotch
Terminal=false
X-GNOME-Autostart-enabled=true
X-XFCE-Autostart-Override=true
"""


def _path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / _NAME


def _exec_command() -> str:
    found = shutil.which("codenotch")
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "bin" / "codenotch"
    if local.is_file():
        return shlex.quote(sys.executable) + " " + shlex.quote(str(local))
    return shlex.quote(sys.executable) + " -m codenotch"


def is_enabled() -> bool:
    return _path().is_file()


def enable() -> str:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_TEMPLATE.format(exec_cmd=_exec_command()), encoding="utf-8")
    return f"wrote {p}"


def disable() -> str:
    p = _path()
    p.unlink(missing_ok=True)
    return f"removed {p}"
