"""Provider glyphs (ported from glyphs.rs). No vendor logo is drawn by hand: a user override in
~/.config/codenotch/glyphs/<id>.svg|.png wins, else the built-in @lobehub/icons-static-svg artwork
(MIT, see glyphs/NOTICE.md), else the page falls back to a letter. SVGs are inlined into the page,
so <script> blocks, on* handlers and javascript: URLs are stripped first.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from . import config

IDS = ("claude", "codex", "cursor", "grok", "gemini")
BUILTIN_DIR = Path(__file__).resolve().parent / "glyphs"
MAX_BYTES = 512 * 1024

_SCRIPT_RE = re.compile(r"<script\b.*?(</script\s*>|$)", re.IGNORECASE | re.DOTALL)
_ON_ATTR_RE = re.compile(r"\s+on[a-z0-9]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL_RE = re.compile(r"(href|xlink:href)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)
_FOREIGN_RE = re.compile(r"<foreignObject\b.*?(</foreignObject\s*>|$)", re.IGNORECASE | re.DOTALL)


def sanitize_svg(s: str) -> str:
    s = _SCRIPT_RE.sub("", s)
    s = _FOREIGN_RE.sub("", s)
    s = _ON_ATTR_RE.sub("", s)
    s = _JS_URL_RE.sub("", s)
    return s


def user_dir() -> Path:
    d = config.config_dir() / "glyphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _from_file(p: Path):
    try:
        data = p.read_bytes()
    except OSError:
        return None
    if not data or len(data) > MAX_BYTES:
        return None
    ext = p.suffix.lower()
    if ext == ".svg":
        return {"kind": "svg", "svg": sanitize_svg(data.decode("utf-8", errors="replace")),
                "url": "", "source": str(p)}
    if ext == ".png":
        return {"kind": "png", "svg": "", "source": str(p),
                "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}
    return None


def collect() -> dict:
    out = {}
    udir = user_dir()
    for gid in IDS:
        g = None
        for ext in (".svg", ".png"):
            g = _from_file(udir / f"{gid}{ext}")
            if g:
                break
        if g is None:
            g = _from_file(BUILTIN_DIR / f"{gid}.svg")
            if g:
                g["source"] = f"builtin:{gid}.svg"
        if g:
            out[gid] = g
    return out
