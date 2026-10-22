"""Merges codenotch-hook into ~/.claude/settings.json without touching the user's own hooks
(ported from hooks_install.rs). Our entries are the ones whose command names codenotch-hook, and a
backup of the file is written before every change."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

WIRING = (
    ("SessionStart", False, "session_start"),
    ("UserPromptSubmit", False, "running"),
    ("PreToolUse", True, "running"),
    ("PostToolUse", True, "running"),
    ("Notification", False, "attention"),
    ("Stop", False, "done"),
    ("SessionEnd", False, "session_end"),
)


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def hook_executable():
    found = shutil.which("codenotch-hook")
    if found:
        return Path(found)
    local = Path(__file__).resolve().parent.parent / "bin" / "codenotch-hook"
    return local if local.is_file() else None


def _is_ours(entry) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and isinstance(h.get("command"), str) and "codenotch-hook" in h["command"]
        for h in hooks)


def _load(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _backup_and_write(path: Path, root: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.codenotch-bak-{int(time.time())}"))
    path.write_text(json.dumps(root, indent=2), encoding="utf-8")


def is_installed() -> bool:
    try:
        return "codenotch-hook" in settings_path().read_text(encoding="utf-8")
    except OSError:
        return False


def install() -> str:
    exe = hook_executable()
    if exe is None:
        raise RuntimeError("codenotch-hook not found (install the .deb, or run from the source tree)")
    path = settings_path()
    root = _load(path)
    if not isinstance(root.get("hooks"), dict):
        root["hooks"] = {}
    for event, need_matcher, internal in WIRING:
        arr = root["hooks"].get(event)
        arr = [e for e in (arr if isinstance(arr, list) else []) if not _is_ours(e)]
        entry = {"hooks": [{"type": "command", "command": f'"{exe}" {internal}', "timeout": 5}]}
        if need_matcher:
            entry["matcher"] = "*"
        arr.append(entry)
        root["hooks"][event] = arr
    _backup_and_write(path, root)
    return f"wrote {path} ({len(WIRING)} events)"


def uninstall() -> str:
    path = settings_path()
    if not path.exists():
        return "settings.json does not exist, nothing to uninstall"
    root = _load(path)
    hooks = root.get("hooks")
    if not isinstance(hooks, dict):
        return "no hooks configuration found"
    removed = 0
    for k, v in list(hooks.items()):
        if isinstance(v, list):
            kept = [e for e in v if not _is_ours(e)]
            removed += len(v) - len(kept)
            hooks[k] = kept
    _backup_and_write(path, root)
    return f"removed {removed} Codenotch hook(s)"
