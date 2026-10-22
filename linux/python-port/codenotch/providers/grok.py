"""Grok usage adapter, ported from the Windows port's grok.rs.

Credential: Grok CLI's own session at ~/.grok/auth.json, keyed by
"<issuer>::<client_id>". Only a session minted by https://auth.x.ai is used —
the file can also hold a customer IdP token meant for that customer's private
proxy, so entries are filtered by issuer before ever being sent anywhere.

Endpoint: GET https://cli-chat-proxy.grok.com/v1/billing?format=credits
(Authorization: Bearer …, X-XAI-Token-Auth: xai-grok-cli) — the same one the
CLI's own `/usage` asks. Read only.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from ..models import LimitWindow, UsageSnapshot
from ..net import HttpError, get_allow_status

name = "grok"
poll_secs = 300
ENDPOINT = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
TRUSTED_ISSUER = "https://auth.x.ai"


def open_url() -> str:
    return "https://x.ai/usage"


def auth_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def present() -> bool:
    return auth_path().is_file()


def _iso_ms(v) -> Optional[int]:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


class _Creds:
    __slots__ = ("token", "expires_at", "email")

    def __init__(self, token: str, expires_at: int, email: Optional[str]):
        self.token = token
        self.expires_at = expires_at
        self.email = email

    def is_expired(self) -> bool:
        return self.expires_at > 0 and self.expires_at <= config.now_ms()


def _is_trusted(key: str, entry: dict) -> bool:
    issuer = key.split("::", 1)[0]
    return issuer == TRUSTED_ISSUER or entry.get("oidc_issuer") == TRUSTED_ISSUER


def pick(root: dict) -> Optional[_Creds]:
    trusted = []
    for key, entry in root.items():
        if not isinstance(entry, dict) or not _is_trusted(key, entry):
            continue
        token = entry.get("key")
        if not token:
            continue
        trusted.append(_Creds(token, _iso_ms(entry.get("expires_at")) or 0, entry.get("email")))
    now = config.now_ms()
    for i, c in enumerate(trusted):
        if c.expires_at == 0 or c.expires_at > now:
            return trusted.pop(i)
    return trusted[0] if trusted else None


def _read_credentials() -> Optional[_Creds]:
    try:
        root = json.loads(auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(root, dict):
        return None
    return pick(root)


def _pct(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return max(0.0, min(1.0, float(v) / 100.0))
    except (TypeError, ValueError):
        return None


def humanize(name_: str) -> str:
    out = []
    for i, c in enumerate(name_):
        if c.isupper() and i > 0:
            out.append(" ")
        out.append(c)
    return "".join(out)


def parse_credits(v: dict):
    cfg = v.get("config")
    if not isinstance(cfg, dict):
        return [], "Grok returned no billing config"
    current = cfg.get("currentPeriod") or {}
    resets_at = _iso_ms(current.get("end")) or _iso_ms(cfg.get("billingPeriodEnd"))

    out = []
    products = cfg.get("productUsage") if isinstance(cfg.get("productUsage"), list) else None
    headline_label = None
    if products:
        first = products[0]
        p = first.get("product") if isinstance(first, dict) else None
        if p:
            headline_label = humanize(p)

    used = _pct(cfg.get("creditUsagePercent"))
    if used is not None:
        out.append(LimitWindow(id="credits", label=headline_label or "Grok Build", used=used, resets_at=resets_at))
    elif products:
        for product in products:
            u = _pct(product.get("usagePercent")) if isinstance(product, dict) else None
            if u is None:
                continue
            wire = product.get("product")
            label = humanize(wire) if wire else "Usage"
            id_ = "credits" if not out else (wire or label)
            out.append(LimitWindow(id=id_, label=label, used=u, resets_at=resets_at))

    if not out:
        period_type = current.get("type")
        if isinstance(period_type, str) and "WEEKLY" in period_type:
            out.append(LimitWindow(id="credits", label="Weekly limit", used=0.0, resets_at=resets_at))

    if not out:
        return out, "Grok has nothing metered on this account yet"
    return out, ""


def read_once(prev: UsageSnapshot) -> UsageSnapshot:
    snap = UsageSnapshot(status=prev.status, windows=list(prev.windows), note=prev.note,
                          fetched_at=prev.fetched_at, backoff_until=prev.backoff_until)
    creds = _read_credentials()
    if creds is None:
        snap.status = "needsAuth"
        snap.note = "Run grok login — it signs in and refreshes the token this reads."
        return snap
    try:
        headers = {"Authorization": f"Bearer {creds.token}", "X-XAI-Token-Auth": "xai-grok-cli",
                   "Accept": "application/json"}
        resp = get_allow_status(ENDPOINT, headers=headers)
        if resp.status_code in (401, 403):
            snap.status = "needsAuth"
            snap.note = "Grok session was rejected — run grok login again"
            return snap
        if resp.status_code == 429:
            snap.status = "error" if not snap.windows else "stale"
            snap.note = "Grok is rate limiting; the last reading stands"
            return snap
        if resp.status_code >= 400 or resp.json is None:
            raise HttpError(f"HTTP {resp.status_code}")
        windows, note = parse_credits(resp.json)
        snap.fetched_at = config.now_ms()
        if not windows:
            snap.status = "none"
            snap.windows = []
            snap.note = note
        else:
            snap.status = "ok"
            snap.windows = windows
            snap.note = f"{creds.email} · via Grok CLI" if creds.email else "via Grok CLI"
    except HttpError as e:
        snap.status = "error" if not snap.windows else "stale"
        snap.note = str(e)
    return snap
