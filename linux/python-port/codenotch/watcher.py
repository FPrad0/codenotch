"""Transcript watcher (ported from watcher.rs): infers Claude Code session state from appends to
~/.claude*/projects/**/*.jsonl, for sessions whose hooks are not installed.
  - the file is being appended                              → running
  - last line = plain assistant text, quiet for > 2.5 s     → done
  - last line = assistant tool_use, quiet for > 20 s        → attention (inferred)
Polled rather than inotify-driven: a cheap mtime check on recently active files each second, and
a full rescan of the roots every 15 s to find new sessions.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config
from .sessions import HookEvent

QUIET_DONE_MS = 2_500
QUIET_ATTN_MS = 20_000
QUIET_USER_DONE_MS = 75_000
QUIET_OTHER_MS = 5 * 60_000
RESCAN_SECS = 15
FRESH_WINDOW_MS = 10 * 60 * 1000
TAIL_BYTES = 256 * 1024


def roots():
    from .providers import claude
    return [p.dir / "projects" for p in claude.profiles()]


def is_session_jsonl(p: Path) -> bool:
    if p.suffix != ".jsonl" or p.name == "audit.jsonl":
        return False
    return "subagents" not in p.parts


def _user_text(v: dict):
    c = (v.get("message") or {}).get("content")
    if isinstance(c, str):
        s = c.strip()
        return s[:120] if s else None
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                s = b["text"].strip()
                if s:
                    return s[:120]
    return None


def tail_info(path: Path):
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return None
    entry, prompt, model = None, "", ""
    lines = [l for l in raw.decode("utf-8", errors="replace").splitlines() if l.strip()]
    for line in reversed(lines[-80:]):
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if not isinstance(v, dict):
            continue
        t = v.get("type")
        if t not in ("user", "assistant"):
            continue
        if entry is None:
            entry = v
        if not model and t == "assistant":
            m = (v.get("message") or {}).get("model")
            if isinstance(m, str):
                model = m
        if not prompt and t == "user":
            prompt = _user_text(v) or ""
        if entry is not None and model and prompt:
            break
    if entry is None:
        return None
    return entry, prompt, model


class _Track:
    __slots__ = ("session", "cwd", "last_append", "kind", "sent", "interrupted", "prompt", "model", "mtime")

    def __init__(self):
        self.session = ""
        self.cwd = ""
        self.last_append = 0
        self.kind = "other"
        self.sent = ""
        self.interrupted = False
        self.prompt = ""
        self.model = ""
        self.mtime = 0.0


class Watcher:
    def __init__(self, on_event):
        self.on_event = on_event
        self.tracks: dict = {}
        self.fresh: dict = {}  # path -> mtime of files worth watching closely

    def start(self):
        threading.Thread(target=self._run, name="watcher", daemon=True).start()

    def _run(self):
        last_scan = 0.0
        while True:
            try:
                if time.monotonic() - last_scan > RESCAN_SECS:
                    last_scan = time.monotonic()
                    self._rescan()
                for p, known in list(self.fresh.items()):
                    try:
                        m = p.stat().st_mtime
                    except OSError:
                        self.fresh.pop(p, None)
                        continue
                    if m > known:
                        self.fresh[p] = m
                        self._ingest(p)
                self._evaluate()
            except Exception as e:
                config.log(f"watcher: {e}")
            time.sleep(1.0)

    def _rescan(self):
        cutoff = time.time() - FRESH_WINDOW_MS / 1000
        for root in roots():
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "subagents"]
                for fn in filenames:
                    if not fn.endswith(".jsonl"):
                        continue
                    p = Path(dirpath) / fn
                    if not is_session_jsonl(p):
                        continue
                    try:
                        m = p.stat().st_mtime
                    except OSError:
                        continue
                    if m < cutoff:
                        continue
                    if p not in self.fresh:
                        self.fresh[p] = m
                        self._ingest(p)
        for p in [p for p, m in self.fresh.items() if m < cutoff]:
            self.fresh.pop(p, None)

    def _ingest(self, path: Path):
        info = tail_info(path)
        if info is None:
            return
        v, prompt, model = info
        session = v.get("sessionId") if isinstance(v.get("sessionId"), str) else path.stem
        if not session:
            return
        cwd = v.get("cwd") if isinstance(v.get("cwd"), str) else ""
        t = v.get("type")
        if t == "user":
            kind = "user"
        elif t == "assistant":
            content = (v.get("message") or {}).get("content")
            has_tool = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            kind = "tool" if has_tool else "text"
        else:
            kind = "other"
        tr = self.tracks.setdefault(path, _Track())
        tr.session = session
        if cwd:
            tr.cwd = cwd
        tr.last_append = config.now_ms()
        tr.kind = kind
        tr.interrupted = t == "user" and "interrupt" in json.dumps(v.get("message") or {}).lower()
        if prompt:
            tr.prompt = prompt
        if model:
            tr.model = model
        tr.sent = "running"
        self._push("running", tr)

    def _evaluate(self):
        now = config.now_ms()
        for tr in self.tracks.values():
            if not tr.last_append:
                continue
            quiet = now - tr.last_append
            target = None
            if tr.kind == "text" and quiet > QUIET_DONE_MS:
                target = "done"
            elif tr.kind == "tool" and quiet > QUIET_ATTN_MS:
                target = "attention"
            elif tr.kind == "user" and tr.interrupted and quiet > QUIET_DONE_MS:
                target = "done"
            elif tr.kind == "user" and quiet > QUIET_USER_DONE_MS:
                target = "done"
            elif tr.kind == "other" and quiet > QUIET_OTHER_MS:
                target = "done"
            if target and tr.sent != target:
                tr.sent = target
                self._push(target, tr)

    def _push(self, e: str, tr: _Track):
        self.on_event(HookEvent(e=e, session_id=tr.session, cwd=tr.cwd, prompt=tr.prompt,
                                model=tr.model, src="watch"))
