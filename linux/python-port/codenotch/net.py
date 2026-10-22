"""Small HTTP helper so every provider makes the same kind of request."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from . import __version__

TIMEOUT_SECS = 15
USER_AGENT = f"codenotch/{__version__} (Linux)"


@dataclass
class HttpResult:
    status_code: int
    json: Optional[dict]
    text: str
    headers: dict


class HttpError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def get(url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT_SECS) -> HttpResult:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        resp = requests.get(url, headers=hdrs, timeout=timeout)
    except requests.RequestException as e:
        raise HttpError(str(e)) from e
    body_json = None
    try:
        body_json = resp.json()
    except ValueError:
        pass
    if resp.status_code >= 400:
        raise HttpError(f"HTTP {resp.status_code}", status_code=resp.status_code)
    return HttpResult(resp.status_code, body_json, resp.text, dict(resp.headers))


def get_allow_status(url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT_SECS):
    """Like get(), but returns the response even on 4xx/5xx instead of raising,
    so callers can branch on 401/403/429 the way the Windows port does."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        resp = requests.get(url, headers=hdrs, timeout=timeout)
    except requests.RequestException as e:
        raise HttpError(str(e)) from e
    body_json = None
    try:
        body_json = resp.json()
    except ValueError:
        pass
    return HttpResult(resp.status_code, body_json, resp.text, dict(resp.headers))
