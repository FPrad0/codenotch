"""Which window a provider's ring shows — main.rs ring_window, notch.html headlineOf."""
from __future__ import annotations


def _tightest(ws):
    best = None
    for w in ws:
        if w.count is not None:
            continue
        if best is None or w.used > best.used or (w.used == best.used and w.id < best.id):
            best = w
    return best


def _lane_family(w) -> str:
    i = w.id.lower()
    return "gemini" if i.startswith("gemini") else ("3p" if i.startswith(("3p", "claude")) else "")


def _lane_is(w, limit: str) -> bool:
    t = f"{w.id} {w.label}".lower()
    if limit == "weekly":
        return "weekly" in t
    return any(k in t for k in ("5h", "5-hour", "five hour", "five-hour", "hourly", "session"))


def ring_window(pid, windows, limit, model):
    def by(i):
        return next((w for w in windows if w.id == i), None)
    if pid == "claude":
        return by("session")
    if pid == "codex":
        return by("primary")
    if pid == "cursor":
        return by("included") or by("api")
    if pid == "grok":
        return by("credits") or (windows[0] if windows else None)
    if pid == "glm":
        return by("session")
    fam = [w for w in windows if _lane_family(w) == model]
    lanes = fam or list(windows)
    if limit != "automatic":
        w = _tightest([w for w in lanes if _lane_is(w, limit)])
        if w:
            return w
    return _tightest([w for w in lanes if w.used < 1]) or _tightest(lanes) or (lanes[0] if lanes else None)
