"""The tray icon: the notch's own ring (same track, same colour ramp) drawn with Cairo."""
from __future__ import annotations

import math
from pathlib import Path

import cairo

from . import config

SIZE = 48
AMPLE, WATCH, CRIT = (0x00, 0xFF, 0x88), (0xF2, 0xFF, 0x00), (0xFF, 0x3F, 0x00)


def _tone(f: float):
    c = CRIT if f >= 0.8 else WATCH if f >= 0.5 else AMPLE
    return tuple(v / 255 for v in c)


def render_ring(percent: float, status: str = "ok") -> Path:
    """Returns a PNG path. The name rotates: AppIndicator caches an icon by its path."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIZE, SIZE)
    ctx = cairo.Context(surface)
    c = SIZE / 2
    ctx.set_source_rgba(0.04, 0.04, 0.04, 0.92)
    ctx.arc(c, c, SIZE / 2 - 1, 0, 2 * math.pi)
    ctx.fill()
    ctx.set_line_width(SIZE * 0.13)
    r = SIZE / 2 - SIZE * 0.14
    ctx.set_source_rgba(0.19, 0.19, 0.19, 1)
    ctx.arc(c, c, r, 0, 2 * math.pi)
    ctx.stroke()
    used = max(0.0, min(1.0, percent / 100.0))
    if status not in ("absent", "needsAuth", "error") and used > 0:
        red, green, blue = _tone(used)
        ctx.set_source_rgba(red, green, blue, 0.55 if status == "stale" else 1.0)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.arc(c, c, r, -math.pi / 2, -math.pi / 2 + used * 2 * math.pi)
        ctx.stroke()
    idx = int(config.now_ms() / 1000) % 4
    out = config.cache_dir() / f"tray_{idx}.png"
    surface.write_to_png(str(out))
    return out
