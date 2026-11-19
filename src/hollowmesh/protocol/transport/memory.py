"""P5 — MemoryTransport: in-process delivery for the sim (Doc 2 §1.4).
P7 — the `relay` hook: every delivery passes through it, so the sim's lying relay
can alter or drop records in transit (and the dashboard can draw particles).

Holds a ref to the sim's network and calls the peer node's sync handlers directly.
Reachability comes only from the sim (world.reachability()); this class never decides it.

The sim provides a Network with two methods (anything with these works):
    reachable(node_id) -> list[str]   who node_id can talk to right now
    node(node_id)      -> the node    the Node object for that id
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..record import Record
from ..sync import (
    PushRecords,
    PushResult,
    SyncNode,
    SyncRequest,
    SyncResponse,
    handle_push,
    handle_sync,
)

# relay(sender, receiver, records) -> the records that actually arrive.
# Return them unchanged to just watch; return altered copies (never mutate the
# originals, they're shared) or fewer of them to lie.
Relay = Callable[[str, str, list[Record]], list[Record]]


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

    def __init__(self, node_id: str, network: Network, relay: Relay | None = None) -> None:
        self.node_id = node_id
        self.network = network
        self.relay = relay

    def reachable(self) -> list[str]:
        return self.network.reachable(self.node_id)

    def sync(self, peer: str, req: SyncRequest) -> SyncResponse:
        resp = handle_sync(self._peer(peer), req)
        if self.relay is not None:  # records flow peer -> us
            resp = {**resp, "records": self.relay(peer, self.node_id, resp["records"])}
        return resp

    def push(self, peer: str, msg: PushRecords) -> PushResult:
        node = self._peer(peer)
        if self.relay is not None:  # records flow us -> peer
            msg = {**msg, "records": self.relay(self.node_id, peer, msg["records"])}
        return handle_push(node, msg)

    def _peer(self, peer: str) -> SyncNode:
        # Like a real network: a message to someone you can't reach doesn't arrive.
        if peer not in self.network.reachable(self.node_id):
            raise Unreachable(f"{self.node_id[:8]} can't reach {peer[:8]}")
        return self.network.node(peer)
