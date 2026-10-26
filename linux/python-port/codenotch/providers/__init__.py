from __future__ import annotations

from typing import Protocol

from ..models import UsageSnapshot


class Provider(Protocol):
    """Every provider module exposes this shape (duck-typed, not enforced)."""

    name: str
    poll_secs: int

    def present(self) -> bool: ...

    def read_once(self, prev: UsageSnapshot) -> UsageSnapshot: ...

    def open_url(self) -> str: ...
