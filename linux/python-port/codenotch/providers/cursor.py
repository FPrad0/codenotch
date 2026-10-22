"""Cursor usage adapter, ported from the Windows port's cursor.rs.

Borrows the editor's own session instead of managing a login:
  1. Credential: Cursor's global state DB (SQLite, table ItemTable(key,value))
     at ~/.config/Cursor/User/globalStorage/state.vscdb (Linux path — the
     editor is Electron/VS Code-based and follows XDG on Linux, just as it
     follows %APPDATA% on Windows and ~/Library/Application Support on macOS).
     Reads cursorAuth/accessToken + cursorAuth/stripeMembershipAuthId, joined
     into the cookie WorkosCursorSessionToken=<authId>::<token>.
  2. Endpoint: GET https://cursor.com/api/usage-summary (Cookie + Accept: json)
     0% is a real reading (the free plan keeps used/limit at 0 and reports
     the allowance under breakdown.bonus instead) — never treat 0 as "no data".
Read only; the token never reaches logs or the UI.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot
from ..net import HttpError, get_allow_status

name = "cursor"
poll_secs = 300
ENDPOINT = "https://cursor.com/api/usage-summary"


def open_url() -> str:
    return "https://cursor.com/dashboard?tab=usage"


def _state_db_path() -> Path:
    base = Path(__import__("os").environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def present() -> bool:
    return _state_db_path().is_file()


def _open_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.execute("SELECT 1 FROM ItemTable LIMIT 1")
        return conn
    except sqlite3.Error:
        pass
    try:
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=1.0)
        conn.execute("SELECT 1 FROM ItemTable LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _item(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0]:
        return row[0]
    return None


class _Creds:
    __slots__ = ("cookie", "plan")

    def __init__(self, cookie: str, plan: Optional[str]):
        self.cookie = cookie
        self.plan = plan


def _read_credentials() -> Optional[_Creds]:
    path = _state_db_path()
    conn = _open_ro(path)
    if conn is None:
        return None
    try:
        token = _item(conn, "cursorAuth/accessToken")
        auth_id = _item(conn, "cursorAuth/stripeMembershipAuthId")
        if not token or not auth_id:
            return None
        plan = _item(conn, "cursorAuth/stripeMembershipType")
        return _Creds(f"WorkosCursorSessionToken={auth_id}::{token}", plan)
    finally:
        conn.close()


def _pct(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return max(0.0, min(1.0, float(v) / 100.0))
    except (TypeError, ValueError):
        return None


def _parse_iso(v) -> Optional[int]:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def parse_summary(v: dict):
    resets_at = _parse_iso(v.get("billingCycleEnd"))
    usage = v.get("individualUsage") or {}
    plan = usage.get("plan") or {}
    out = []
    total = _pct(plan.get("totalPercentUsed"))
    if total is not None:
        out.append(LimitWindow(id="included", label="Included usage", used=total, resets_at=resets_at))
    api = _pct(plan.get("apiPercentUsed"))
    if api is not None and api > 0.0:
        out.append(LimitWindow(id="api", label="API usage", used=api, resets_at=resets_at))
    od = usage.get("onDemand") or {}
    if od.get("enabled") and (od.get("limit") or 0) > 0 and od.get("used") is not None:
        out.append(LimitWindow(
            id="on_demand", label="On demand",
            used=max(0.0, min(1.0, od["used"] / od["limit"])),
            resets_at=resets_at,
        ))
    if out:
        return out, ""
    membership = v.get("membershipType") or "this"
    if v.get("isUnlimited") is True:
        note = f"Unlimited on the {membership} plan — nothing to meter"
    else:
        note = f"The {membership} plan has nothing for Cursor to meter yet"
    return out, note


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    snap = UsageSnapshot(status=prev.status, windows=list(prev.windows), note=prev.note,
                          fetched_at=prev.fetched_at, backoff_until=prev.backoff_until)
    creds = _read_credentials()
    if creds is None:
        snap.status = "needsAuth"
        snap.note = "Sign in to Cursor (the editor) to see usage."
        return snap
    try:
        resp = get_allow_status(ENDPOINT, headers={"Cookie": creds.cookie, "Accept": "application/json"})
        if resp.status_code in (401, 403):
            snap.status = "needsAuth"
            snap.note = "Cursor session was rejected — sign in again in the editor"
            return snap
        if resp.status_code >= 400 or resp.json is None:
            raise HttpError(f"HTTP {resp.status_code}")
        windows, note = parse_summary(resp.json)
        snap.fetched_at = config.now_ms()
        if not windows:
            snap.status = "none"
            snap.windows = []
            snap.note = note
        else:
            snap.status = "ok"
            snap.windows = windows
            membership = resp.json.get("membershipType")
            if membership:
                snap.note = f"{_cap(membership)} · via Cursor"
            elif creds.plan:
                snap.note = f"{_cap(creds.plan)} · via Cursor"
            else:
                snap.note = ""
    except HttpError as e:
        snap.status = "error" if not snap.windows else "stale"
        snap.note = str(e)
    return snap
