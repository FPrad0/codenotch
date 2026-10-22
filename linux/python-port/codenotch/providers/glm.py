"""GLM Coding Plan (Z.ai) usage adapter, ported from the Windows port's glm.rs.

Endpoint: GET {console}/api/monitor/usage/quota/limit, authenticated with the raw API key (no
"Bearer"), borrowed from whichever tool already holds one, in order:
  1. ~/.config/codenotch/glm.json {"api_key": "...", "base_url": "https://api.z.ai"}
  2. Claude Code: ~/.claude/settings.json env.ANTHROPIC_AUTH_TOKEN, only when env.ANTHROPIC_BASE_URL
     points at a Z.ai console (otherwise it is somebody's Anthropic key)
  3. ZCode: ~/.zcode/v2/config.json (an enabled *-coding-plan provider), then credentials.json
  4. OpenCode: ~/.local/share/opencode/auth.json
Errors ride in under HTTP 200 ({code:401, success:false}), so the envelope is read first. Windows
are identified by length: unit=3,number=5 is the 5-hour session, unit=6,number=1 the week.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot
from ..net import HttpError, get_allow_status

name = "glm"
poll_secs = 300
BACKOFF_BASE_SECS = 60
BACKOFF_CAP_SECS = 900

_backoff_until = 0
_consecutive_429 = 0


def open_url() -> str:
    return "https://z.ai/manage-apikey/apikey-list"


def request_refresh() -> None:
    global _backoff_until, _consecutive_429
    _backoff_until = 0
    _consecutive_429 = 0


def _key_file() -> Path:
    return config.config_dir() / "glm.json"


def _is_zai_host(host: str) -> bool:
    return host in ("api.z.ai", "open.bigmodel.cn") or host.endswith(".z.ai") or host.endswith(".bigmodel.cn")


def _console_for_host(host: str) -> str:
    return "https://open.bigmodel.cn" if host.endswith("bigmodel.cn") else "https://api.z.ai"


def _host_of(url) -> Optional[str]:
    s = str(url or "").strip()
    if not s:
        return None
    rest = s.split("://", 1)[1] if "://" in s else s
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return host or None


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _non_empty(v) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


class _Cred:
    __slots__ = ("token", "base", "source")

    def __init__(self, token, base, source):
        self.token, self.base, self.source = token, base, source


def _manual_key():
    root = _read_json(_key_file())
    if not isinstance(root, dict):
        return None
    token = _non_empty(root.get("api_key") or root.get("apiKey"))
    if not token:
        return None
    host = _host_of(root.get("base_url") or root.get("baseURL"))
    base = _console_for_host(host) if host and _is_zai_host(host) else "https://api.z.ai"
    return _Cred(token, base, "glm.json")


def _claude_code_key():
    root = _read_json(Path.home() / ".claude" / "settings.json")
    env = root.get("env") if isinstance(root, dict) else None
    if not isinstance(env, dict):
        return None
    token = _non_empty(env.get("ANTHROPIC_AUTH_TOKEN")) or _non_empty(env.get("ANTHROPIC_API_KEY"))
    host = _host_of(env.get("ANTHROPIC_BASE_URL"))
    if not token or not host or not _is_zai_host(host):
        return None
    return _Cred(token, _console_for_host(host), "Claude Code")


def _zcode_plan_key():
    root = _read_json(Path.home() / ".zcode" / "v2" / "config.json")
    providers = root.get("provider") if isinstance(root, dict) else None
    if not isinstance(providers, dict):
        return None
    for pid in sorted(providers):
        p = providers[pid]
        if "coding-plan" not in pid or not isinstance(p, dict) or p.get("enabled") is False:
            continue
        opts = p.get("options") if isinstance(p.get("options"), dict) else {}
        key = _non_empty(opts.get("apiKey"))
        if not key:
            continue
        host = _host_of(opts.get("baseURL"))
        base = _console_for_host(host) if host and _is_zai_host(host) else "https://api.z.ai"
        return _Cred(key, base, "ZCode")
    return None


def _zcode_oauth():
    root = _read_json(Path.home() / ".zcode" / "v2" / "credentials.json")
    token = _non_empty(root.get("oauth:zai:access_token")) if isinstance(root, dict) else None
    if not token or token.startswith("enc:v1:"):
        return None
    return _Cred(token, "https://api.z.ai", "ZCode")


def _opencode_key():
    ids = ["zai-coding-plan", "zai", "z-ai", "z.ai", "glm", "zhipu", "zhipuai"]
    for p in (Path.home() / ".local" / "share" / "opencode" / "auth.json",
              Path.home() / ".config" / "opencode" / "auth.json"):
        root = _read_json(p)
        if not isinstance(root, dict):
            continue
        for pid in ids:
            entry = root.get(pid)
            base = "https://open.bigmodel.cn" if pid.startswith("zhipu") else "https://api.z.ai"
            token = _non_empty(entry)
            if token:
                return _Cred(token, base, "OpenCode")
            if isinstance(entry, dict):
                for field in ("apiKey", "api_key", "token", "key", "accessToken", "auth_token"):
                    token = _non_empty(entry.get(field))
                    if token:
                        return _Cred(token, base, "OpenCode")
    return None


def _load_credential():
    for fn in (_manual_key, _claude_code_key, _zcode_plan_key, _zcode_oauth, _opencode_key):
        c = fn()
        if c:
            return c
    return None


def present() -> bool:
    h = Path.home()
    return (_key_file().is_file() or _claude_code_key() is not None
            or (h / ".zcode" / "v2" / "config.json").is_file()
            or (h / ".zcode" / "v2" / "credentials.json").is_file()
            or (h / ".local" / "share" / "opencode" / "auth.json").is_file()
            or (h / ".config" / "opencode" / "auth.json").is_file())


def _window_id(limit: dict) -> str:
    ty = limit.get("type") or ""
    if ty == "TIME_LIMIT":
        return "mcp"
    unit, number = limit.get("unit"), limit.get("number")
    if unit == 3 and number == 5:
        return "session"
    if unit == 6 and number == 1:
        return "weekly"
    if isinstance(unit, int) and isinstance(number, int):
        return f"window-{unit}x{number}"
    return ty.lower() or "unknown"


def _label_for(wid: str, unit, number) -> str:
    if wid == "session":
        return "Current session"
    if wid == "weekly":
        return "Weekly"
    if wid == "mcp":
        return "MCP (1 month)"
    if wid.startswith("window-"):
        if unit == 3:
            return f"Usage ({number or 0} h)"
        if unit == 6:
            return f"Usage ({number or 0} wk)"
    return "Usage"


def windows_from(v: dict) -> list:
    limits = ((v.get("data") or {}).get("limits")) if isinstance(v, dict) else None
    if not isinstance(limits, list):
        return []
    out = []
    for l in limits:
        if not isinstance(l, dict) or not isinstance(l.get("percentage"), (int, float)):
            continue
        wid = _window_id(l)
        reset = l.get("nextResetTime")
        out.append(LimitWindow(
            id=wid, label=_label_for(wid, l.get("unit"), l.get("number")),
            used=max(0.0, min(1.0, l["percentage"] / 100.0)),
            resets_at=int(max(0, reset)) if isinstance(reset, (int, float)) else None,
        ))
    rank = {"session": 0, "weekly": 1, "mcp": 2}
    out.sort(key=lambda w: (rank.get(w.id, 3), w.id))
    return out


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    global _backoff_until, _consecutive_429
    snap = UsageSnapshot(status=prev.status, windows=list(prev.windows), note=prev.note,
                          fetched_at=prev.fetched_at, backoff_until=prev.backoff_until)
    now = config.now_ms()
    if _backoff_until > now:
        snap.backoff_until = _backoff_until
        snap.note = f"Rate limited — retrying in {(_backoff_until - now) // 1000}s"
        return snap
    cred = _load_credential()
    if cred is None:
        if not present():
            return UsageSnapshot(status="absent")
        snap.status = "needsAuth"
        snap.note = "No usable Z.ai key found (ZCode, OpenCode or a glm.json key file)"
        return snap
    try:
        resp = get_allow_status(f"{cred.base}/api/monitor/usage/quota/limit",
                                headers={"Authorization": cred.token, "Accept": "application/json"})
    except HttpError as e:
        snap.status = "error" if not snap.windows else "stale"
        snap.note = f"Live read failed ({e})"
        return snap
    if resp.status_code in (401, 403):
        snap.status = "needsAuth"
        snap.note = "Z.ai rejected the key — renew it in the tool that holds it"
        return snap
    if resp.status_code == 429:
        _consecutive_429 += 1
        try:
            ra = int(resp.headers.get("retry-after", "0"))
        except ValueError:
            ra = 0
        wait = max(min(max(BACKOFF_BASE_SECS * 2 ** min(_consecutive_429 - 1, 4), BACKOFF_BASE_SECS),
                       BACKOFF_CAP_SECS), ra)
        _backoff_until = config.now_ms() + wait * 1000
        snap.backoff_until = _backoff_until
        snap.note = f"Rate limited — retrying in {wait}s"
        return snap
    v = resp.json
    if resp.status_code >= 400 or not isinstance(v, dict):
        snap.status = "error" if not snap.windows else "stale"
        snap.note = f"Live read failed (HTTP {resp.status_code})"
        return snap
    code = v.get("code") if isinstance(v.get("code"), int) else 0
    if code in (401, 403) or (v.get("success") is not True and code != 200):
        if code in (401, 403):
            snap.status = "needsAuth"
            snap.note = "Z.ai rejected the key — renew it in the tool that holds it"
        else:
            snap.status = "error"
            snap.note = f"Z.ai monitor refused the request ({code})"
        return snap
    windows = windows_from(v)
    if not windows:
        snap.status = "stale"
        snap.note = "The plan reported no usage windows"
        return snap
    level = (v.get("data") or {}).get("level")
    _consecutive_429 = 0
    snap.status = "ok"
    snap.windows = windows
    snap.fetched_at = config.now_ms()
    snap.backoff_until = 0
    snap.note = (f"{level[:1].upper()}{level[1:]} · via {cred.source}" if isinstance(level, str) and level
                 else f"via {cred.source}")
    return snap
