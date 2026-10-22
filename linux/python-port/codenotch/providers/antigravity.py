"""Antigravity usage adapter, ported from the Windows port's agy_cli.rs.

Official CLI (`agy`) is the only source in this Linux port — see
README-linux.md "Known gaps" for why the legacy Credential-Manager/local
bridge fallback (Windows/macOS-specific) is not carried over.

Runs `agy --sandbox --print-timeout 30s --print /usage` and parses its text
report, e.g.:
    Quota:
    Gemini Models Weekly Limit Remaining 94% 2026-09-12T01:47:23Z
    Gemini Models Five Hour Limit Remaining 78% 2026-09-10T06:12:31Z
    Claude and GPT models Weekly Limit Remaining 100% 2026-09-17T01:47:23Z
    Claude and GPT models Five Hour Limit Remaining 100% 2026-09-10T08:13:44Z
Each row becomes one window: id "<group> <window>", used = 100 - remaining%,
grouped under <group> so the hover/menu can box a model family together.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot

name = "antigravity"
poll_secs = 300
_MIN_REFRESH_GAP_MS = 5 * 60 * 1000

_LINE_RE = re.compile(
    r"^(?P<group>.+?) (?P<window>Weekly Limit|Five Hour Limit) Remaining "
    r"(?P<pct>-?\d+(?:\.\d+)?)% (?P<reset>\S+)$"
)


def open_url() -> str:
    return "https://antigravity.google/"


def find_agy() -> Optional[Path]:
    local = Path.home() / ".local" / "share" / "agy" / "bin" / "agy"
    if local.is_file():
        return local
    which = shutil.which("agy")
    return Path(which) if which else None


def state_roots() -> list:
    """Every ~/.gemini/antigravity* install (the IDE's own state, used for activity only)."""
    base = Path.home() / ".gemini"
    try:
        return sorted(p for p in base.iterdir() if p.name.startswith("antigravity") and p.is_dir())
    except OSError:
        return []


def present() -> bool:
    return find_agy() is not None or bool(state_roots())


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _parse_iso(v: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def parse_quota(text: str) -> list:
    clean = _strip_ansi(text)
    out = []
    for line in clean.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            pct = float(m.group("pct"))
        except ValueError:
            continue
        if not (0.0 <= pct <= 100.0):
            continue
        group = " ".join(m.group("group").split())
        window = m.group("window")
        resets_at = _parse_iso(m.group("reset"))
        if resets_at is None or resets_at <= 0:
            continue
        out.append(LimitWindow(
            id=f"{group} {window}",
            label="5-hour Limit" if window == "Five Hour Limit" else "Weekly Limit",
            used=max(0.0, min(1.0, (100.0 - pct) / 100.0)),
            resets_at=resets_at,
            group=group,
        ))
    # Grouped by model family in the order the report names them, the 5-hour lane first
    families = []
    for w in out:
        if w.group not in families:
            families.append(w.group)
    out.sort(key=lambda w: (families.index(w.group), 0 if w.label.startswith("5") else 1))
    return out


def _run_agy(agy: Path) -> str:
    result = subprocess.run(
        [str(agy), "--sandbox", "--print-timeout", "30s", "--print", "/usage"],
        capture_output=True, text=True, timeout=70, cwd=str(Path.home()),
    )
    return result.stdout or ""


_last_attempt_ms = 0


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    global _last_attempt_ms
    snap = UsageSnapshot(status=prev.status, windows=list(prev.windows), note=prev.note,
                          fetched_at=prev.fetched_at, backoff_until=prev.backoff_until)
    agy = find_agy()
    if agy is None:
        if not state_roots():
            return UsageSnapshot(status="absent")
        snap.status = "stale" if snap.windows else "needsAuth"
        snap.note = "Install the Antigravity CLI (agy) to read its quota"
        return snap

    now = config.now_ms()
    if now - _last_attempt_ms < _MIN_REFRESH_GAP_MS and snap.windows:
        return snap
    _last_attempt_ms = now

    try:
        text = _run_agy(agy)
    except (subprocess.TimeoutExpired, OSError) as e:
        snap.status = "stale" if snap.windows else "error"
        snap.note = f"Live read failed ({type(e).__name__})"
        return snap

    windows = parse_quota(text)
    if not windows:
        snap.status = "stale" if snap.windows else "needsAuth"
        snap.note = "Waiting for Antigravity CLI quota"
        return snap

    snap.status = "ok"
    snap.windows = windows
    snap.fetched_at = now
    snap.note = "via Antigravity CLI"
    return snap
