"""P5 — MemoryTransport: in-process delivery for the sim (Doc 2 §1.4).

Holds a ref to the sim's network and calls the peer node's sync handlers directly.
Reachability comes only from the sim (world.reachability()); this class never decides it.

The sim provides a Network with two methods (anything with these works):
    reachable(node_id) -> list[str]   who node_id can talk to right now
    node(node_id)      -> the node    the Node / FakeNode object for that id
"""

from __future__ import annotations

from typing import Protocol

from ..sync import (
    PushRecords,
    PushResult,
    SyncNode,
    SyncRequest,
    SyncResponse,
    handle_push,
    handle_sync,
)


class Network(Protocol):
    """What MemoryTransport needs from the sim (the teammate's side of P6)."""

    def reachable(self, node_id: str) -> list[str]: ...
    def node(self, node_id: str) -> SyncNode: ...


class Unreachable(ConnectionError):
    """The peer can't be reached right now (link cut, blackout, partition)."""


class MemoryTransport:
    """One per node. Delivers sync messages by calling the peer's handlers directly.

    Records are passed by reference, not copied: nodes never modify records, so sharing
    them is safe and keeps 300 nodes cheap (Doc 2 §1.3's shared pool).
    """

    def __init__(self, node_id: str, network: Network) -> None:
        self.node_id = node_id
        self.network = network

    def reachable(self) -> list[str]:
        return self.network.reachable(self.node_id)

    def sync(self, peer: str, req: SyncRequest) -> SyncResponse:
        return handle_sync(self._peer(peer), req)

    def push(self, peer: str, msg: PushRecords) -> PushResult:
        return handle_push(self._peer(peer), msg)

    def _peer(self, peer: str) -> SyncNode:
        # Like a real network: a message to someone you can't reach doesn't arrive.
        if peer not in self.network.reachable(self.node_id):
            raise Unreachable(f"{self.node_id[:8]} can't reach {peer[:8]}")
        return self.network.node(peer)
