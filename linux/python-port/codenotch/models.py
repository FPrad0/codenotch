"""Shared data shapes, ported from the Windows/macOS Codenotch clients.

A LimitWindow is one usage bar (e.g. "Current session", "Weekly limit").
A UsageSnapshot is one provider's whole reading: its windows plus a status
that says how much to trust them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class LimitWindow:
    id: str
    label: str
    used: float = 0.0          # 0.0-1.0 fraction used
    resets_at: Optional[int] = None   # ms epoch
    count: Optional[int] = None       # a pure count window with no denominator
    derived: bool = False             # the number is ours, not the vendor's
    group: Optional[str] = None       # heading this window is filed under on the card

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LimitWindow":
        return LimitWindow(
            id=d.get("id", ""),
            label=d.get("label", ""),
            used=d.get("used", 0.0),
            resets_at=d.get("resets_at"),
            count=d.get("count"),
            derived=d.get("derived", False),
            group=d.get("group"),
        )


# status: ok | stale | needsAuth | backoff | error | none | absent
@dataclass
class UsageSnapshot:
    status: str = "absent"
    windows: list = field(default_factory=list)   # list[LimitWindow]
    fetched_at: int = 0
    note: str = ""
    backoff_until: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "windows": [w.to_dict() for w in self.windows],
            "fetched_at": self.fetched_at,
            "note": self.note,
            "backoff_until": self.backoff_until,
        }

    @staticmethod
    def from_dict(d: dict) -> "UsageSnapshot":
        return UsageSnapshot(
            status=d.get("status", "absent"),
            windows=[LimitWindow.from_dict(w) for w in d.get("windows", [])],
            fetched_at=d.get("fetched_at", 0),
            note=d.get("note", ""),
            backoff_until=d.get("backoff_until", 0),
        )

    def headline(self) -> Optional[LimitWindow]:
        return self.windows[0] if self.windows else None
