"""Paths, config.json and persisted readings. The config keys and defaults are the Windows
port's (config.rs), so the pages read the same values they were written against.

Everything lives in $XDG_CONFIG_HOME/codenotch (~/.config/codenotch by default).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .models import UsageSnapshot

APP_NAME = "codenotch"
SIZES = (0.8, 1.0, 1.25)
EDGES = ("left", "right", "top", "bottom")
LOG_MAX_BYTES = 1024 * 1024

DEFAULTS = {
    "port": 48666,
    "lang": "auto",
    "notch_along": {},
    "notch_edge": "right",
    "notch_monitor": None,
    "scale": 1.0,
    "weekly_ring": "off",
    "theme": "system",
    "notch_slots": [],
    "antigravity_limit": "automatic",
    "antigravity_model": "gemini",
    "notch_visible": True,
    "notch_on_hover": True,
    "tray_visible": True,
    "show_move_handle": True,
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def now_ms() -> int:
    return int(time.time() * 1000)


def snap_scale(scale) -> float:
    try:
        s = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if s != s:
        return 1.0
    if s < 0.9:
        return SIZES[0]
    if s < 1.125:
        return SIZES[1]
    return SIZES[2]


def edge_or_right(value) -> str:
    return value if value in EDGES else "right"


def edge_is_vertical(edge: str) -> bool:
    return edge in ("left", "right")


class Config:
    """The settings, guarded by a lock: the pollers read them from their own threads."""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = dict(DEFAULTS)
        self.data["notch_along"] = {}
        raw = None
        try:
            raw = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS:
                    self.data[k] = v
        self._normalise()

    def _normalise(self):
        d = self.data
        d["notch_edge"] = edge_or_right(d.get("notch_edge"))
        d["scale"] = snap_scale(d.get("scale"))
        if d.get("weekly_ring") not in ("off", "inside", "outside"):
            d["weekly_ring"] = "off"
        if d.get("theme") not in ("system", "light", "dark"):
            d["theme"] = "system"
        if d.get("antigravity_limit") not in ("automatic", "5h", "weekly"):
            d["antigravity_limit"] = "automatic"
        if d.get("antigravity_model") not in ("gemini", "3p"):
            d["antigravity_model"] = "gemini"
        if not isinstance(d.get("notch_along"), dict):
            d["notch_along"] = {}
        slots = d.get("notch_slots")
        d["notch_slots"] = [
            {"provider": s["provider"]} for s in (slots if isinstance(slots, list) else [])
            if isinstance(s, dict) and isinstance(s.get("provider"), str)
        ]
        try:
            d["port"] = int(d.get("port") or 48666)
        except (TypeError, ValueError):
            d["port"] = 48666
        for k in ("notch_visible", "notch_on_hover", "tray_visible", "show_move_handle"):
            d[k] = bool(d.get(k))
        if not d["notch_visible"]:
            d["tray_visible"] = True

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def set(self, key, value, save=True):
        with self.lock:
            self.data[key] = value
            self._normalise()
            if save:
                self.save()

    def along(self, edge: str) -> float:
        with self.lock:
            v = self.data["notch_along"].get(edge, 0.5)
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    def set_along(self, edge: str, along: float) -> None:
        with self.lock:
            self.data["notch_along"][edge] = max(0.0, min(1.0, float(along)))
            self.save()

    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.data, indent=2)
        tmp = config_path().with_suffix(".json.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, config_path())
        except OSError as e:
            log(f"config: could not save ({e})")


_STALE_ON_LOAD = {"codex", "cursor", "grok", "antigravity", "glm-usage"}


def load_snapshot(name: str) -> UsageSnapshot:
    try:
        data = json.loads((config_dir() / f"{name}.json").read_text(encoding="utf-8"))
        snap = UsageSnapshot.from_dict(data)
    except (OSError, ValueError, AttributeError):
        return UsageSnapshot()
    if name in _STALE_ON_LOAD and snap.windows:
        snap.status = "stale"
    return snap


def persist_snapshot(name: str, snap: UsageSnapshot) -> None:
    try:
        (config_dir() / f"{name}.json").write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        pass


_log_lock = threading.Lock()


def log(line: str) -> None:
    p = config_dir() / "run.log"
    with _log_lock:
        try:
            mode = "w" if p.exists() and p.stat().st_size > LOG_MAX_BYTES else "a"
            with p.open(mode, encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass
