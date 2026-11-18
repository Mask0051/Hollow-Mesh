"""P5 — MemoryTransport: in-process delivery for the sim (Doc 2 §1.4). Due Wed 4 PM.

Holds a ref to the sim's network and calls the peer node's sync handler directly.
Reachability comes only from the sim (world.reachability()); this class never decides it.
"""

from __future__ import annotations

from typing import Any

from ..sync import PushRecords, PushResult, SyncRequest, SyncResponse


class MemoryTransport:
    def __init__(self, node_id: str, network: Any) -> None:
        raise NotImplementedError("P5")

    def reachable(self) -> list[str]:
        raise NotImplementedError("P5")

    def sync(self, peer: str, req: SyncRequest) -> SyncResponse:
        raise NotImplementedError("P5")

    def push(self, peer: str, msg: PushRecords) -> PushResult:
        raise NotImplementedError("P5")
