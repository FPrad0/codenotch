"""Claude usage adapter, ported from the Windows port's usage.rs (itself ported
from upstream Codenotch's documented behaviour).

Endpoint: GET https://api.anthropic.com/api/oauth/usage
Headers: Authorization: Bearer <token>; anthropic-beta: oauth-2025-04-20; 15s timeout

Rules:
  - credential comes from Claude Code's own store (~/.claude/.credentials.json
    or ~/.claude-<slug>/.credentials.json for extra accounts), read only
  - 401 -> re-read the credential once and retry (Claude Code may have just
    refreshed it) -> still failing means needsAuth; 403 is access denied
  - 429 -> back off 60s * 2^n capped at 15 min, Retry-After only raises it;
    the deadline is persisted
  - an expired token is never sent: renewed by running the standalone
    `claude -p` with empty stdin shortly before it expires. Only that CLI
    writes ~/.claude/.credentials.json.
  - never invent a percentage on failure: keep the last reading marked stale
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot
from ..net import HttpError, get_allow_status

name = "claude"
poll_active_secs = 60
poll_idle_secs = 300

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CRED_NAMES = (".credentials.json", "credentials.json")
BACKOFF_BASE_SECS = 60
BACKOFF_CAP_SECS = 900
RENEW_MARGIN_MS = 4 * 60 * 1000
RENEW_COOLDOWN_MS = 10 * 60 * 1000
RENEW_RETRY_CAP_MS = 60 * 60 * 1000
RENEW_TIMEOUT_SECS = 30
EXPIRED_NOTE = "Credential expired — run claude once in a terminal to renew it"

_renew_lock = threading.Lock()
_renew_busy = False


def open_url() -> str:
    return "https://claude.ai/settings/usage"


# ---------------- accounts ----------------

class Profile:
    __slots__ = ("dir", "slug")

    def __init__(self, directory: Path, slug: Optional[str]):
        self.dir = directory
        self.slug = slug

    def key(self) -> str:
        return str(self.dir)

    def account_name(self) -> str:
        return self.slug or "default"

    def group(self, plan: Optional[str]) -> str:
        if plan:
            return f"{self.account_name()} · {plan}"
        return self.account_name()


def _has_credential(directory: Path) -> bool:
    return any((directory / n).is_file() for n in CRED_NAMES)


def profiles() -> list:
    home = Path.home()
    out = [Profile(home / ".claude", None)]
    extra = []
    try:
        for entry in home.iterdir():
            n = entry.name
            if not n.startswith(".claude-"):
                continue
            slug = n[len(".claude-"):]
            if not slug or not entry.is_dir() or not _has_credential(entry):
                continue
            extra.append(Profile(entry, slug))
    except OSError:
        pass
    extra.sort(key=lambda p: p.slug)
    out.extend(extra)
    return out


class Credential:
    __slots__ = ("token", "expires_at", "plan")

    def __init__(self, token: str, expires_at: Optional[int], plan: Optional[str]):
        self.token = token
        self.expires_at = expires_at
        self.plan = plan

    def expired(self, now: int) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def _read_credentials(directory: Path) -> Optional[Credential]:
    for name_ in CRED_NAMES:
        p = directory / name_
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            v = json.loads(text)
        except ValueError:
            continue
        oauth = v.get("claudeAiOauth", v)
        token = oauth.get("accessToken")
        if not token or not str(token).strip():
            continue
        expires_at = oauth.get("expiresAt")
        try:
            expires_at = int(expires_at) if expires_at is not None else None
        except (TypeError, ValueError):
            expires_at = None
        plan = oauth.get("subscriptionType")
        return Credential(token, expires_at, plan)
    return None


# ---------------- token renewal ----------------

_DESKTOP_OWNED_HINTS = ("anthropicclaude", "claude/claude-code", "claude-desktop")


def _is_desktop_owned(p: Path) -> bool:
    s = str(p).lower().replace("\\", "/")
    return any(h in s for h in _DESKTOP_OWNED_HINTS)


def find_cli() -> Optional[Path]:
    candidates = []
    home = Path.home()
    candidates.append(home / ".local" / "bin" / "claude")
    candidates.append(home / ".npm-global" / "bin" / "claude")
    candidates.append(home / ".volta" / "bin" / "claude")
    which = shutil.which("claude")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK) and not _is_desktop_owned(c):
            return c
    return None


class _RenewState:
    def __init__(self):
        self.attempted_for: Optional[int] = None
        self.last_attempt: Optional[int] = None
        self.failures = 0


_renew_states: dict = {}


def _retry_wait_ms(failures: int) -> int:
    return min(RENEW_COOLDOWN_MS * (2 ** min(failures, 16)), RENEW_RETRY_CAP_MS)


def _should_renew(expires_at, now, attempted_for, last_attempt, failures) -> bool:
    if expires_at is None:
        return False
    if expires_at > now + RENEW_MARGIN_MS:
        return False
    if last_attempt is None:
        return True
    wait = _retry_wait_ms(failures) if attempted_for == expires_at else RENEW_COOLDOWN_MS
    return (now - last_attempt) >= wait


def _run_renewal(cli: Path, directory: Path) -> None:
    env = dict(os.environ)
    for k in list(env.keys()):
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"):
            env.pop(k, None)
    env["CLAUDE_CONFIG_DIR"] = str(directory)
    try:
        subprocess.run(
            [str(cli), "-p"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=RENEW_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass


def _maybe_renew(cred: Credential, directory: Path, who: str) -> Optional[bool]:
    global _renew_busy
    if not _renew_lock.acquire(blocking=False):
        return None
    try:
        if _renew_busy:
            return None
        now = config.now_ms()
        st = _renew_states.setdefault(str(directory), _RenewState())
        if not _should_renew(cred.expires_at, now, st.attempted_for, st.last_attempt, st.failures):
            return None
        if st.attempted_for != cred.expires_at:
            st.failures = 0
        st.last_attempt = now
        st.attempted_for = cred.expires_at
        st.failures += 1
        cli = find_cli()
        if cli is None:
            config.log(f"claude[{who}]: token about to expire and no standalone claude CLI found to renew it")
            return False
        _renew_busy = True
    finally:
        _renew_lock.release()
    try:
        _run_renewal(cli, directory)
    finally:
        with _renew_lock:
            _renew_busy = False
    after = _read_credentials(directory)
    renewed = bool(after and after.expires_at and cred.expires_at and after.expires_at > cred.expires_at)
    config.log(f"claude[{who}]: {'token renewed' if renewed else 'ran claude -p but expiry did not move'} via {cli}")
    return renewed


# ---------------- parsing ----------------

def _parse_reset(v) -> Optional[int]:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _label_for(kind: str) -> str:
    labels = {
        "session": "Current session",
        "seven_day": "Weekly (all models)",
        "weekly_all": "Weekly (all models)",
        "seven_day_opus": "Weekly (Opus)",
        "weekly_opus": "Weekly (Opus)",
        "weekly_scoped": "Weekly (model-scoped)",
    }
    if kind in labels:
        return labels[kind]
    s = kind.replace("_", " ")
    return s[:1].upper() + s[1:] if s else s


def parse_response(v: dict) -> list:
    out = []
    for l in v.get("limits") or []:
        kind = l.get("kind")
        pct = l.get("percent")
        if kind is None or pct is None:
            continue
        resets = _parse_reset(l.get("resets_at"))
        if resets is None:
            continue  # a window without a reset time is not shown
        out.append(LimitWindow(
            id=kind, label=_label_for(kind),
            used=max(0.0, min(1.0, pct / 100.0)),
            resets_at=resets,
        ))

    aliases = [
        ("five_hour", "session", {"session", "five_hour"}),
        ("seven_day", "seven_day", {"seven_day", "weekly_all", "weekly"}),
    ]
    for field_, id_, alias in aliases:
        w = v.get(field_)
        if not isinstance(w, dict):
            continue
        u = w.get("utilization")
        if u is None:
            continue
        used = max(0.0, min(1.0, u / 100.0))
        resets_at = _parse_reset(w.get("resets_at"))
        label = _label_for(id_)
        dup = any(
            x.id in alias or x.label == label or (
                resets_at is not None and x.resets_at is not None
                and resets_at // 1000 == x.resets_at // 1000
                and abs(x.used - used) < 0.005
            )
            for x in out
        )
        if dup:
            continue
        out.append(LimitWindow(id=id_, label=label, used=used, resets_at=resets_at))

    out.sort(key=lambda w: 0 if w.id == "session" else 1)
    return out


def _backoff_secs(consecutive: int, retry_after_floor: int) -> int:
    exp = BACKOFF_BASE_SECS * (2 ** min(consecutive, 4))
    return max(min(max(exp, BACKOFF_BASE_SECS), BACKOFF_CAP_SECS), retry_after_floor)


class _FetchErr(Exception):
    pass


class _NeedsAuth(_FetchErr):
    pass


class _RateLimited(_FetchErr):
    def __init__(self, retry_after: int):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _fetch_once(token: str) -> list:
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    resp = get_allow_status(ENDPOINT, headers=headers)
    if resp.status_code == 401:
        raise _NeedsAuth()
    if resp.status_code == 403:
        raise _FetchErr("Claude HTTP 403: access denied. Check network or account access.")
    if resp.status_code == 429:
        try:
            ra = int(resp.headers.get("retry-after", "0"))
        except ValueError:
            ra = 0
        raise _RateLimited(ra)
    if resp.status_code >= 400:
        raise _FetchErr(f"HTTP {resp.status_code}")
    if resp.json is None:
        raise _FetchErr("could not parse response")
    return parse_response(resp.json)


class _Account:
    def __init__(self):
        self.consecutive_429 = 0
        self.backoff_until = 0
        self.windows: list = []
        self.status = ""
        self.note = ""
        self.fetched_at = 0


_accounts: dict = {}


def _decorate(windows: list, p: Profile, group: Optional[str]) -> list:
    for w in windows:
        if group:
            w.group = group
        if p.slug:
            w.id = f"{w.id}@{p.slug}"
    return windows


def _poll_account(p: Profile, acc: _Account, group: Optional[str]) -> None:
    who = p.account_name()
    cred = _read_credentials(p.dir)
    if cred is not None:
        renewed = _maybe_renew(cred, p.dir, who)
        if renewed:
            acc.consecutive_429 = 0
            acc.backoff_until = 0

    if acc.backoff_until > config.now_ms():
        return

    cred = _read_credentials(p.dir)
    if cred is None:
        acc.status = "needsAuth"
        acc.note = "No Claude Code credential found"
        return
    if cred.expired(config.now_ms()):
        acc.status = "needsAuth" if not acc.windows else "stale"
        acc.note = EXPIRED_NOTE
        return

    try:
        try:
            windows = _fetch_once(cred.token)
        except _NeedsAuth:
            cred2 = _read_credentials(p.dir)
            if cred2 and cred2.token != cred.token:
                windows = _fetch_once(cred2.token)
            else:
                raise
        acc.consecutive_429 = 0
        acc.status = "ok"
        acc.windows = _decorate(windows, p, group)
        acc.fetched_at = config.now_ms()
        acc.note = ""
        acc.backoff_until = 0
    except _NeedsAuth:
        acc.status = "needsAuth"
        acc.note = "Credential rejected (switched accounts?)"
    except _RateLimited as e:
        acc.consecutive_429 += 1
        wait = _backoff_secs(acc.consecutive_429 - 1, e.retry_after)
        acc.note = f"Rate limited, retrying in {wait}s"
        acc.backoff_until = config.now_ms() + wait * 1000
    except (_FetchErr, HttpError) as e:
        if not acc.windows:
            acc.status = "error"
        acc.note = str(e)


def _aggregate(order: list) -> UsageSnapshot:
    rank = {"ok": 0, "stale": 1, "error": 2}
    multi = len(order) > 1
    snap = UsageSnapshot(status="", windows=[])
    notes = []
    best = 4
    for p in order:
        acc = _accounts.get(p.key())
        if acc is None:
            continue
        snap.windows.extend(acc.windows)
        snap.fetched_at = max(snap.fetched_at, acc.fetched_at)
        r = rank.get(acc.status, 3)
        if acc.status and r < best:
            best = r
            snap.status = acc.status
        if acc.note:
            notes.append(f"{p.account_name()}: {acc.note}" if multi else acc.note)
        if acc.backoff_until and (snap.backoff_until == 0 or acc.backoff_until < snap.backoff_until):
            snap.backoff_until = acc.backoff_until
    if not snap.status:
        snap.status = "needsAuth"
    snap.note = " · ".join(notes)
    return snap


def present() -> bool:
    return bool(profiles() and any((p.dir).is_dir() for p in profiles())) or (Path.home() / ".claude").exists()


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    order = profiles()
    multi = len(order) > 1
    for p in order:
        acc = _accounts.setdefault(p.key(), _Account())
        if not acc.windows and prev.windows:
            acc.windows = [w for w in prev.windows
                           if (w.id.endswith("@" + p.slug) if p.slug else "@" not in w.id)]
        group = None
        if multi:
            cred = _read_credentials(p.dir)
            group = p.group(cred.plan if cred else None)
        _poll_account(p, acc, group)
    valid_keys = {p.key() for p in order}
    for k in list(_accounts.keys()):
        if k not in valid_keys:
            del _accounts[k]
    return _aggregate(order)
