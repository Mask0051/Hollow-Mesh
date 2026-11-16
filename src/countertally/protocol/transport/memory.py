"""M5 — MemoryTransport: in-process delivery, with links the sim can cut/heal. Due Tue 9:30 PM."""

from __future__ import annotations

from typing import Any


class MemoryTransport:
    def __init__(self) -> None:
        self.links: set[frozenset[str]] = set()

    def connect(self, a: str, b: str) -> None:
        raise NotImplementedError("M5")

    def cut(self, a: str, b: str) -> None:
        raise NotImplementedError("M5")

    def send(self, src: str, dst: str, msg: dict[str, Any]) -> None:
        raise NotImplementedError("M5")

    def reachable(self, src: str) -> list[str]:
        raise NotImplementedError("M5")
