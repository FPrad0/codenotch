"""Where the notch goes, as pure functions (ported from the Windows port's main.rs).

All figures are GDK logical pixels: GTK does the HiDPI scaling, so unlike Windows there is no
physical/logical juggling here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

NOTCH_W = 360.0
NOTCH_LONG = 650.0
HOT_PAD = 10.0


@dataclass
class Screen:
    id: Optional[str]
    x: int
    y: int
    w: int
    h: int
    work: tuple  # (x, y, w, h)
    label: str = ""

    def area(self):
        wx, wy, ww, wh = self.work
        if ww > 0 and wh > 0:
            return self.work
        return (self.x, self.y, self.w, self.h)


def notch_window_size(edge: str):
    if edge in ("left", "right"):
        return (NOTCH_W, NOTCH_LONG)
    return (NOTCH_LONG, NOTCH_LONG)


def edge_origin(s: Screen, edge: str, ww: int, wh: int, ratio: float):
    """Top-left corner for a window ww×wh whose centre sits at `ratio` along `edge`."""
    ax, ay, aw, ah = s.area()

    def along(span, length):
        v = round(span * ratio - length / 2.0)
        return max(0, min(v, max(span - length, 0)))

    if edge == "left":
        return (ax, ay + along(ah, wh))
    if edge == "top":
        return (ax + along(aw, ww), ay)
    if edge == "bottom":
        return (ax + along(aw, ww), ay + ah - wh)
    return (ax + aw - ww, ay + along(ah, wh))


def along_at(pos: int, length: int, start: int, span: int) -> float:
    """edge_origin run backwards along one axis."""
    return max(0.0, min(1.0, ((pos - start) + length / 2.0) / max(span, 1)))


def edge_at(x: float, y: float, w: float, h: float) -> str:
    """The screen split into four triangles about its centre."""
    left, right, top, bottom = x, w - x, y, h - y
    nearest = min(left, right, top, bottom)
    if nearest == right:
        return "right"
    if nearest == left:
        return "left"
    if nearest == top:
        return "top"
    return "bottom"


def work_insets(s: Screen, x: int, y: int, ww: int, wh: int):
    """How far outside-the-work-area covers each side of a window: top, right, bottom, left."""
    wx, wy, waw, wah = s.work
    if waw <= 0 or wah <= 0:
        return [0, 0, 0, 0]
    return [
        max(0, min(wy - y, wh)),
        max(0, min((x + ww) - (wx + waw), ww)),
        max(0, min((y + wh) - (wy + wah), wh)),
        max(0, min(wx - x, ww)),
    ]


def screen_at(screens, x: float, y: float) -> Optional[Screen]:
    for s in screens:
        if s.x <= x < s.x + s.w and s.y <= y < s.y + s.h:
            return s
    return None


def same_screen(a: Screen, b: Screen) -> bool:
    return (a.x, a.y, a.w, a.h) == (b.x, b.y, b.w, b.h)


def cursor_in_hot(rects, lx: float, ly: float, window=None) -> bool:
    """Is the cursor on something the window is there for? Rects are window-relative x,y,w,h."""
    if not rects:
        return False
    if window is not None:
        w, h = window
        if not (0 <= lx < w and 0 <= ly < h):
            return False
    for r in rects:
        if r[0] - HOT_PAD <= lx < r[0] + r[2] + HOT_PAD and r[1] - HOT_PAD <= ly < r[1] + r[3] + HOT_PAD:
            return True
    if len(rects) > 1:
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        return x0 <= lx < x1 and y0 <= ly < y1
    return False


def input_region_rects(rects, win_w: int, win_h: int, pad: float = HOT_PAD):
    """The rectangles that take clicks: each hot rect padded, plus the bounding box of all of them
    (the gap between pill and card counts as inside), clipped to the window."""
    out = []
    if not rects:
        return out

    def clip(x0, y0, x1, y1):
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(win_w, int(round(x1))), min(win_h, int(round(y1)))
        if x1 > x0 and y1 > y0:
            out.append((x0, y0, x1 - x0, y1 - y0))

    for r in rects:
        clip(r[0] - pad, r[1] - pad, r[0] + r[2] + pad, r[1] + r[3] + pad)
    if len(rects) > 1:
        clip(min(r[0] for r in rects), min(r[1] for r in rects),
             max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects))
    return out
