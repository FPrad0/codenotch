"""Working-state detection for the non-Claude providers (ported from activity.rs). Polled every 2 s and
reported only on change; databases are re-queried only when their file (or -wal) changed.
  - Cursor: composerHeaders rows in state.vscdb — unfinishedRunAt = running, pending actions = waiting
  - Codex: thread_turns in ~/.codex/thread_history_1.sqlite, else the last rollout entry
  - Antigravity: a brain transcript written within the last 45 s
The Windows port's Claude-desktop network heuristic has nothing to watch on Linux and is left out.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from .providers import codex, cursor

INTERVAL_SECS = 2.0
ANTIGRAVITY_STALE_MS = 45_000


def _mtime_ms(p: Path) -> int:
    try:
        return int(p.stat().st_mtime * 1000)
    except OSError:
        return 0


def _open_ro(path: Path):
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05, check_same_thread=False)
    except sqlite3.Error:
        return None


class _DbCache:
    def __init__(self, path_fn):
        self.path_fn = path_fn
        self.conn = None
        self.sig = (0, 0)
        self.last: list = []

    def refresh(self, query):
        path = self.path_fn()
        if path is None:
            return []
        sig = (_mtime_ms(path), _mtime_ms(Path(str(path) + "-wal")))
        if sig == self.sig and self.conn is not None:
            return self.last
        self.sig = sig
        if self.conn is None:
            self.conn = _open_ro(path)
            if self.conn is None:
                return []
        try:
            self.last = query(self.conn) or []
        except sqlite3.Error:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None
            self.last = []
        return self.last


def _act(provider, state, name, detail, since):
    return {"provider": provider, "state": state, "name": name, "detail": detail, "since": int(since)}


def _cursor_query(conn):
    out = []
    rows = conn.execute(
        "SELECT value FROM composerHeaders WHERE isArchived = 0 ORDER BY recency DESC LIMIT 40").fetchall()
    for (raw,) in rows:
        try:
            v = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict) or not isinstance(v.get("composerId"), str):
            continue
        blocked = v.get("hasBlockingPendingActions") is True or v.get("hasPendingPlan") is True
        running = v.get("unfinishedRunAt")
        if blocked:
            state = "waiting"
        elif isinstance(running, (int, float)):
            state = "busy"
        else:
            continue
        since = running if isinstance(running, (int, float)) else (
            v.get("lastUpdatedAt") or v.get("createdAt") or config.now_ms())
        out.append(_act("cursor", state, v.get("name") or "Untitled chat",
                        "needs your input" if blocked else (v.get("subtitle") or "Working"), since))
    out.sort(key=lambda a: -a["since"])
    return out


def _codex_turns_query(names_conn):
    def query(conn):
        now = config.now_ms()
        out = []
        rows = conn.execute("SELECT thread_id, started_at FROM thread_turns WHERE status = 'inProgress' "
                            "ORDER BY started_at DESC LIMIT 8").fetchall()
        for thread_id, started in rows:
            try:
                started = float(started)
            except (TypeError, ValueError):
                started = 0
            started_ms = int(started if started > 10_000_000_000 else started * 1000)
            row = conn.execute("SELECT created_at_ms, item_type FROM thread_items WHERE thread_id = ? "
                               "ORDER BY created_at_ms DESC LIMIT 1", (thread_id,)).fetchone()
            last_ms = int(row[0]) if row and row[0] is not None else started_ms
            last_type = (row[1] or "").lower() if row else ""
            if now - last_ms > 10 * 60_000 and now - started_ms > 2 * 60_000:
                continue
            name = ""
            if names_conn is not None:
                try:
                    r = names_conn.execute("SELECT COALESCE(title,''), COALESCE(first_user_message,''), "
                                           "COALESCE(agent_nickname,'') FROM threads WHERE id = ?",
                                           (thread_id,)).fetchone()
                except sqlite3.Error:
                    r = None
                if r:
                    title, first, nick = r
                    name = title.strip() and title or (first.strip() and first[:40]) or (
                        nick.strip() and f"Agent {nick}") or ""
            waiting = any(k in last_type for k in ("approval", "permission", "request_user"))
            out.append(_act("codex", "waiting" if waiting else "busy", name or "Codex",
                            "needs your input" if waiting else "Working", started_ms))
        return out
    return query


def _codex_last_step(lines):
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if not isinstance(v, dict):
            continue
        ts = 0
        if isinstance(v.get("timestamp"), str):
            try:
                ts = int(datetime.fromisoformat(v["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                ts = 0
        kind = v.get("type")
        p = v.get("payload") if isinstance(v.get("payload"), dict) else {}
        pt = p.get("type")
        step = None
        if kind == "turn_context":
            step = "thinking"
        elif kind == "response_item":
            if pt in ("function_call", "local_shell_call", "custom_tool_call", "web_search_call"):
                step = "tool"
            elif pt in ("function_call_output", "custom_tool_call_output", "reasoning"):
                step = "thinking"
            elif pt == "message":
                step = {"assistant": "asst", "user": "thinking"}.get(p.get("role"))
        elif kind == "event_msg":
            if pt in ("turn_aborted", "task_complete"):
                step = "aborted"
            elif pt in ("task_started", "item_started", "exec_command_begin", "user_message",
                        "agent_reasoning", "agent_reasoning_raw_content"):
                step = "thinking"
            elif pt == "agent_message":
                step = "asst"
        if step:
            return step, ts
    return None


class ActivityProbe:
    def __init__(self, on_change):
        self.on_change = on_change
        self.last: list = []
        self.cursor = _DbCache(lambda: cursor._state_db_path())
        self.codex_turns = _DbCache(lambda: Path.home() / ".codex" / "thread_history_1.sqlite")
        self.codex_names = None
        self.rollout_path = None
        self.rollout_checked = 0
        self.rollout_sig = 0
        self.rollout_last: list = []

    def start(self):
        threading.Thread(target=self._run, name="activity", daemon=True).start()

    def current(self) -> list:
        return list(self.last)

    def _codex(self):
        if self.codex_names is None:
            self.codex_names = _open_ro(Path.home() / ".codex" / "state_5.sqlite")
        turns = self.codex_turns.refresh(_codex_turns_query(self.codex_names))
        if turns:
            return turns
        now = config.now_ms()
        if now - self.rollout_checked > 30_000 or self.rollout_path is None:
            self.rollout_checked = now
            self.rollout_path = codex._newest_rollout()
        if self.rollout_path is None:
            return []
        m = _mtime_ms(self.rollout_path)
        if m == self.rollout_sig:
            return [a for a in self.rollout_last if now - a["since"] <= 10 * 60_000]
        self.rollout_sig = m
        self.rollout_last = []
        found = _codex_last_step(codex._tail_lines(self.rollout_path))
        if found:
            step, ts = found
            at = max(ts, m)
            quiet = now - at
            busy = {"tool": quiet <= 10 * 60_000, "thinking": quiet <= 120_000,
                    "asst": quiet <= 4_000, "aborted": False}[step]
            if busy:
                self.rollout_last = [_act("codex", "busy", "Codex", "Working", at)]
        return self.rollout_last

    @staticmethod
    def _antigravity():
        from .providers import antigravity
        newest = 0
        for root in antigravity.state_roots():
            brain = root / "brain"
            if not brain.is_dir():
                continue
            for e in brain.iterdir():
                m = _mtime_ms(e / ".system_generated" / "logs" / "transcript.jsonl")
                newest = max(newest, m)
        if not newest or config.now_ms() - newest > ANTIGRAVITY_STALE_MS:
            return []
        return [_act("gemini", "busy", "Antigravity", "Working", newest)]

    def _read_all(self):
        out = []
        if cursor.present():
            out.extend(self.cursor.refresh(_cursor_query))
        if codex.present():
            out.extend(self._codex())
        out.extend(self._antigravity())
        return out

    def _run(self):
        while True:
            try:
                now = self._read_all()
                if now != self.last:
                    self.last = now
                    self.on_change(now)
            except Exception as e:
                config.log(f"activity: {e}")
            time.sleep(INTERVAL_SECS)
