"""P5 — Version-vector gossip sync (Doc 2 §1.4).

One anti-entropy round (A starts with B):
  A -> B  SyncRequest   {from: A.node_id, vv: {author: head_seq}, limit: 500}
  B -> A  SyncResponse  {from: B.node_id, vv: {...}, records: [raw...], more: bool}
  A       ingest(records); compute what B lacks (A.vv[a] > B.vv[a])
  A -> B  PushRecords   {from: A.node_id, records: [raw...]}   (same limit)

(SyncResponse's signed vv is for withholding proof, which is out of scope — Doc 3 §3.)

Batching: at most 500 records or 512 KB per message, round-robin across authors,
each author's records in seq order from vv[a]+1.
Peer selection: each tick (sim) or every 2 s (HTTP), 2 reachable peers, least
recently synced first; seeded RNG in the sim.
Push on publish: a node's own new records go out right away to up to 3 reachable peers.

Merge rule: a record is applied only through Node.ingest, never written directly.
Write the version-vector merge tests yourself first (Doc 4 §2).
"""

from __future__ import annotations

from typing import Any

from .node import Node


def handle_message(node: Node, msg: dict[str, Any]) -> dict[str, Any] | None:
    """Apply an incoming sync message; return a reply message if one is needed."""
    raise NotImplementedError("P5")


def sync_pair(a: Node, b: Node) -> int:
    """One full request/response/push round between two nodes. Returns records moved."""
    raise NotImplementedError("P5")
