"""Transports move sync messages between nodes. Node logic never knows which one is in use."""

from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    def send(self, src: str, dst: str, msg: dict[str, Any]) -> None: ...

    def reachable(self, src: str) -> list[str]:
        """Peers `src` can currently talk to (the sim controls this for MemoryTransport)."""
        ...
