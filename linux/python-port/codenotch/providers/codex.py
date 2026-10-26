"""Codex usage adapter, ported from the Windows port's codex.rs.

Live: borrow ~/.codex/auth.json (tokens.access_token + tokens.account_id) and
GET https://chatgpt.com/backend-api/wham/usage. The reply carries
rate_limit.{primary_window,secondary_window} with used_percent /
limit_window_seconds / reset_at (seconds) or reset_after_seconds.

Fallback (no native `codex app-server` recovery in this Linux port — see
README-linux.md "Known gaps"): the last rate_limits snapshot at the tail of
the most recent rollout log under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
That reading is from the last run, so it is marked stale by the line's own
timestamp rather than the moment it was read.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot
from ..net import HttpError, get_allow_status

name = "codex"
poll_secs = 300
ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
BACKOFF_MIN_SECS = 60
CURRENT_FOR_MS = 5 * 60 * 1000


def open_url() -> str:
    return "https://chatgpt.com/codex"


def _codex_home() -> Path:
    return Path.home() / ".codex"


def _auth_path() -> Path:
    return _codex_home() / "auth.json"


def present() -> bool:
    return _auth_path().is_file() or (_codex_home() / "sessions").is_dir()


def _b64_decode(part: str) -> Optional[bytes]:
    pad = "=" * (-len(part) % 4)
    try:
        return base64.urlsafe_b64decode(part + pad)
    except (ValueError, TypeError):
        return None


def _jwt_claims(token: str) -> Optional[dict]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    raw = _b64_decode(parts[1])
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class _Credential:
    __slots__ = ("access_token", "account_id", "plan", "expired")

    def __init__(self, access_token, account_id, plan, expired):
        self.access_token = access_token
        self.account_id = account_id
        self.plan = plan
        self.expired = expired


def _load_credential() -> Optional[_Credential]:
    try:
        v = json.loads(_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = v.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = (tokens.get("access_token") or "").strip()
    account_id = (tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return None
    claims = _jwt_claims(access_token) or {}
    expired = False
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        expired = exp * 1000.0 <= config.now_ms()
    plan = None
    id_claims = _jwt_claims(tokens.get("id_token")) if tokens.get("id_token") else None
    if id_claims:
        plan = ((id_claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type"))
    return _Credential(access_token, account_id, plan, expired)


def _label_for(window_minutes: Optional[float], id_: str) -> str:
    if window_minutes and window_minutes > 0:
        if window_minutes < 60:
            return f"{int(window_minutes)}m limit"
        if window_minutes < 60 * 24:
            return f"{int(window_minutes / 60)}h limit"
        days = round(window_minutes / (60 * 24))
        return {7: "Weekly limit", 30: "Monthly limit"}.get(days, f"{days}d limit")
    return "Current session" if id_ == "primary" else "Longer window"


def _secs_to_ms(s) -> Optional[int]:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if s != s or s < 0:  # NaN or negative
        return None
    return int(s * 1000)


def _reset_at_ms(w: dict, now: int, epoch_key: str, delay_key: str) -> Optional[int]:
    ms = _secs_to_ms(w.get(epoch_key))
    if ms is not None:
        return ms
    delay = _secs_to_ms(w.get(delay_key))
    return now + delay if delay is not None else None


def _window_from(w, id_: str, fallback: str, now: int, group: Optional[str] = None) -> Optional[LimitWindow]:
    if not isinstance(w, dict):
        return None
    pct = w.get("used_percent")
    if pct is None:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    window_secs = w.get("limit_window_seconds")
    minutes = (window_secs / 60.0) if isinstance(window_secs, (int, float)) else None
    return LimitWindow(
        id=id_, label=_label_for(minutes, fallback),
        used=max(0.0, min(1.0, pct / 100.0)),
        resets_at=_reset_at_ms(w, now, "reset_at", "reset_after_seconds"),
        group=group,
    )


def _names_spark(extra) -> bool:
    if not isinstance(extra, dict):
        return False
    return any(isinstance(extra.get(k), str) and "spark" in extra[k].lower()
               for k in ("limit_name", "metered_feature"))


def _append_extra(rl, primary_id, secondary_id, group, now, out):
    if not isinstance(rl, dict):
        return
    for key, id_, fallback in (("primary_window", primary_id, "primary"),
                               ("secondary_window", secondary_id, "secondary")):
        w = _window_from(rl.get(key), id_, fallback, now, group)
        if w and not any(x.id == w.id for x in out):
            out.append(w)


def windows_from_usage(v: dict) -> list:
    """Primary and secondary feed the ring; Spark and Code review are extra rows on the card."""
    now = config.now_ms()
    out = []
    rl = v.get("rate_limit") or {}
    for id_, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = _window_from(rl.get(key), id_, id_, now)
        if w:
            out.append(w)
    extras = v.get("additional_rate_limits")
    if isinstance(extras, list):
        for extra in extras:
            if _names_spark(extra):
                _append_extra(extra.get("rate_limit"), "spark", "spark-secondary", "Spark", now, out)
    _append_extra(v.get("code_review_rate_limit"), "code-review", "code-review-secondary",
                  "Code review", now, out)
    return out


def _fetch_usage(cred: _Credential) -> dict:
    headers = {
        "Authorization": f"Bearer {cred.access_token}",
        "ChatGPT-Account-Id": cred.account_id,
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store",
    }
    resp = get_allow_status(ENDPOINT, headers=headers)
    if resp.status_code in (401, 403):
        raise _NeedsAuth()
    if resp.status_code == 429:
        try:
            ra = int(resp.headers.get("retry-after", "0"))
        except ValueError:
            ra = 0
        raise _RateLimited(max(ra, BACKOFF_MIN_SECS))
    if resp.status_code >= 400 or resp.json is None:
        raise HttpError(f"HTTP {resp.status_code}")
    return resp.json


class _FetchErr(Exception):
    pass


class _NeedsAuth(_FetchErr):
    pass


class _RateLimited(_FetchErr):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


# ---------------- Fallback: rollout snapshot ----------------

_RATE_LIMITS_RE = re.compile(r'"rate_limits"')


def _newest_rollout() -> Optional[Path]:
    sessions = _codex_home() / "sessions"
    if not sessions.is_dir():
        return None
    # Bounded scan: the three most-recently-modified date directories, newest file wins.
    date_dirs = []
    try:
        for y in sessions.iterdir():
            if not y.is_dir():
                continue
            for m in y.iterdir():
                if not m.is_dir():
                    continue
                for d in m.iterdir():
                    if d.is_dir():
                        date_dirs.append(d)
    except OSError:
        return None
    date_dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    newest_file, newest_mtime = None, -1.0
    for d in date_dirs[:3]:
        try:
            for f in d.glob("rollout-*.jsonl"):
                mtime = f.stat().st_mtime
                if mtime > newest_mtime:
                    newest_file, newest_mtime = f, mtime
        except OSError:
            continue
    return newest_file


def _tail_lines(path: Path, max_bytes: int = 256 * 1024) -> list:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def _rollout_snapshot(path: Path):
    """Returns (windows, recorded_at_ms, plan) from the last rate_limits line, or None."""
    lines = _tail_lines(path)
    for line in reversed(lines):
        if "rate_limits" not in line:
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        rl = v.get("rate_limits")
        if rl is None:
            payload = v.get("payload") or {}
            rl = payload.get("rate_limits")
        if not isinstance(rl, dict):
            continue
        now = config.now_ms()
        out = []
        for id_ in ("primary", "secondary"):
            w = rl.get(id_)
            if not isinstance(w, dict):
                continue
            pct = w.get("used_percent")
            if pct is None:
                continue
            minutes = w.get("window_minutes")
            resets_at = None
            if isinstance(w.get("resets_at"), (int, float)):
                resets_at = int(w["resets_at"] * 1000)
            elif isinstance(w.get("resets_in_seconds"), (int, float)):
                resets_at = now + int(w["resets_in_seconds"] * 1000)
            out.append(LimitWindow(
                id=id_, label=_label_for(minutes, id_),
                used=max(0.0, min(1.0, float(pct) / 100.0)),
                resets_at=resets_at,
            ))
        timestamp = v.get("timestamp")
        recorded_at = now
        if isinstance(timestamp, str):
            try:
                recorded_at = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                pass
        plan = rl.get("plan_type")
        if out:
            return out, recorded_at, plan
    return None


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    snap = UsageSnapshot(status=prev.status, windows=list(prev.windows), note=prev.note,
                          fetched_at=prev.fetched_at, backoff_until=prev.backoff_until)
    if snap.backoff_until > config.now_ms():
        return snap

    cred = _load_credential()
    rejected = False
    if cred is not None:
        try:
            v = _fetch_usage(cred)
            windows = windows_from_usage(v)
            plan = v.get("plan_type") if isinstance(v.get("plan_type"), str) else cred.plan
            if windows:
                snap.status = "ok"
                snap.windows = windows
                snap.fetched_at = config.now_ms()
                snap.note = _cap(plan) if plan else ""
                snap.backoff_until = 0
                return snap
            snap.note = "Codex reported no usage windows"
        except _NeedsAuth:
            rejected = True  # fall through to the rollout snapshot rather than blanking the cell
            snap.note = ("Codex sign-in expired — open Codex once to refresh it" if cred.expired
                         else "Codex rejected its sign-in — sign in to Codex again")
        except _RateLimited as e:
            snap.backoff_until = config.now_ms() + e.retry_after * 1000
            snap.note = f"Rate limited, retrying in {e.retry_after}s"
            if snap.windows:
                snap.status = "stale"
        except HttpError as e:
            snap.note = f"Live read failed ({e})"

    rollout = _newest_rollout()
    result = _rollout_snapshot(rollout) if rollout is not None else None
    if result is None:
        if cred is None or rejected:
            snap.status = "needsAuth" if not snap.windows else "stale"
            if cred is None:
                snap.note = "Codex has not recorded a usage snapshot yet"
        elif not snap.windows:
            snap.status = "error"
        return snap
    windows, recorded_at, plan = result
    snap.windows = windows
    snap.fetched_at = recorded_at
    age = config.now_ms() - recorded_at
    snap.status = "ok" if age <= CURRENT_FOR_MS else "stale"
    snap.note = f"{_cap(plan)} · from last Codex run" if plan else "from last Codex run"
    return snap


def _cap(s) -> str:
    s = str(s or "")
    return s[:1].upper() + s[1:]
