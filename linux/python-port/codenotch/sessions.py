"""Claude Code session state: attention > running > done > idle (ported from state.rs).

Fed by the hook server (real Claude Code hook events) and the transcript watcher (inference,
used when hooks are not installed). A session with fresh hook data ignores the watcher.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import now_ms

ST_RUNNING, ST_ATTENTION, ST_DONE, ST_IDLE = "running", "attention", "done", "idle"

RUNNING_STALE_MS = 30 * 60 * 1000
DONE_STALE_MS = 24 * 3600 * 1000
IDLE_DROP_MS = 10 * 60 * 1000
ATTENTION_STALE_MS = 24 * 3600 * 1000
HOOK_FRESH_MS = 5 * 60 * 1000
MAX_SESSIONS = 200
MAX_CWD_CHARS = 1024


@dataclass
class HookEvent:
    e: str
    session_id: str
    ppid: int = 0
    cwd: str = ""
    prompt: str = ""
    message: str = ""
    tool_name: str = ""
    tool_cmd: str = ""
    model: str = ""
    src: str = "hook"  # "hook" or "watch"


@dataclass
class Session:
    id: str
    title: str
    state: str = ST_IDLE
    started: int = 0
    total: int = 0
    last: str = ""
    attn: str = ""
    prompt: str = ""
    model: str = ""
    ppid: int = 0
    last_event: int = 0
    cwd: str = ""
    last_hook: int = 0

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title, "state": self.state, "started": self.started,
            "total": self.total, "last": self.last, "attn": self.attn, "prompt": self.prompt,
            "model": self.model,
        }


def _truncate(s: str, n: int) -> str:
    return s[:n] + ("…" if len(s) > n else "")


def title_of(cwd: str, sid: str) -> str:
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else "claude"
    return f"{base} · {sid[:4]}"


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.map: dict = {}

    def apply(self, ev: HookEvent) -> bool:
        now = now_ms()
        with self.lock:
            if ev.e == "session_end":
                return self.map.pop(ev.session_id, None) is not None
            cwd = ev.cwd[:MAX_CWD_CHARS]
            if ev.session_id not in self.map and len(self.map) >= MAX_SESSIONS:
                oldest = min(self.map.values(), key=lambda s: s.last_event)
                self.map.pop(oldest.id, None)
            s = self.map.get(ev.session_id)
            if s is None:
                s = Session(id=ev.session_id, title=title_of(cwd, ev.session_id), started=now,
                            last_event=now, cwd=cwd)
                self.map[ev.session_id] = s
            if ev.src == "watch" and s.last_hook > 0 and now - s.last_hook < HOOK_FRESH_MS:
                return False
            if ev.src == "hook":
                s.last_hook = now
            before = (s.state, s.last, s.attn, s.prompt, s.model)
            s.last_event = now
            if ev.ppid:
                s.ppid = ev.ppid
            if ev.model:
                s.model = ev.model
            if cwd and not s.cwd:
                s.cwd = cwd
                s.title = title_of(cwd, s.id)
            if ev.e == "session_start":
                if s.state != ST_RUNNING:
                    s.state = ST_IDLE
            elif ev.e == "running":
                if s.state != ST_RUNNING:
                    s.started = now
                s.state = ST_RUNNING
                s.attn = ""
                if ev.prompt:
                    s.prompt = _truncate(ev.prompt, 120)
                if ev.tool_name:
                    s.last = (f"🔧 {ev.tool_name}" if not ev.tool_cmd
                              else f"🔧 {ev.tool_name}: {_truncate(ev.tool_cmd, 60)}")
            elif ev.e == "attention":
                s.state = ST_ATTENTION
                if ev.message:
                    s.attn = _truncate(ev.message, 200)
            elif ev.e == "done":
                if s.state != ST_DONE:
                    s.total = max(0, now - s.started)
                s.state = ST_DONE
                s.attn = ""
            return (s.state, s.last, s.attn, s.prompt, s.model) != before

    def dismiss(self, sid: str) -> bool:
        with self.lock:
            return self.map.pop(sid, None) is not None

    def has_active(self) -> bool:
        with self.lock:
            return bool(self.map)

    def sweep(self) -> bool:
        now = now_ms()
        changed = False
        with self.lock:
            for s in self.map.values():
                if s.state == ST_RUNNING and now - s.last_event > RUNNING_STALE_MS:
                    s.state = ST_IDLE
                    changed = True
            before = len(self.map)
            self.map = {
                k: s for k, s in self.map.items()
                if not ((s.state == ST_IDLE and now - s.last_event > IDLE_DROP_MS)
                        or (s.state == ST_DONE and now - s.last_event > DONE_STALE_MS)
                        or (s.state == ST_ATTENTION and now - s.last_event > ATTENTION_STALE_MS))
            }
            return changed or len(self.map) != before

    def snapshot(self, lang: str, lang_resolved: str, clock_24h: bool) -> dict:
        rank = {ST_ATTENTION: 0, ST_RUNNING: 1, ST_DONE: 2}
        with self.lock:
            sessions = sorted(self.map.values(), key=lambda s: (rank.get(s.state, 3), -s.started))
            pub = [s.public() for s in sessions]
        counts = {k: sum(1 for s in pub if s["state"] == k) for k in (ST_ATTENTION, ST_RUNNING, ST_DONE)}
        agg = next((k for k in (ST_ATTENTION, ST_RUNNING, ST_DONE) if counts[k] > 0), ST_IDLE)
        return {
            "sessions": pub, "agg": agg, "counts": counts, "lang": lang,
            "lang_resolved": lang_resolved, "clock_24h": clock_24h, "drag": False,
        }
