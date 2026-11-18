"""Transports move sync messages between nodes (Doc 2 §1.4).

Node logic never knows which one is in use. One transport instance per node.
"""

from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    def reachable(self) -> list[str]:
        """Peer node_ids this node can reach right now (the sim controls this in memory)."""
        ...

    def sync(self, peer: str, req: dict[str, Any]) -> dict[str, Any]:
        """Send a SyncRequest to `peer`, get its SyncResponse."""
        ...

    def push(self, peer: str, msg: dict[str, Any]) -> dict[str, Any]:
        """Send PushRecords to `peer`, get {accepted, pending, rejected}."""
        ...
