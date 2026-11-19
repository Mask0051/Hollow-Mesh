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

Everything here works on any node with node_id / ingest / version_vector /
records_since, so FakeNode and Node gossip the same way (the P6 swap is a class swap).

Who calls what:
    the sim, once per node:   g = Gossiper(node, MemoryTransport(node.node_id, network), rng)
    the sim, each tick:       g.gossip_round()
    a resident posting:       g.publish(type=..., payload=..., now_ms=...)
    MemoryTransport / HTTP:   handle_sync(peer_node, req), handle_push(peer_node, msg)
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from itertools import groupby
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from .node import IngestResult, VersionVector
from .record import Record

if TYPE_CHECKING:
    from .transport import Transport

MAX_BATCH_RECORDS = 500
MAX_BATCH_BYTES = 512 * 1024
PEERS_PER_ROUND = 2
PUSH_FANOUT = 3

# Sync messages (Doc 2 §1.4). Functional TypedDict syntax because `from` is a Python
# keyword and can't be a class attribute. At runtime these are plain dicts / JSON.
SyncRequest = TypedDict("SyncRequest", {"from": str, "vv": VersionVector, "limit": int})
SyncResponse = TypedDict("SyncResponse", {"from": str, "vv": VersionVector, "records": list[Record], "more": bool})
PushRecords = TypedDict("PushRecords", {"from": str, "records": list[Record]})


class Rejected(TypedDict):
    id: str
    reason: str


class PushResult(TypedDict):
    """Reply to PushRecords (same shape as POST /records, Doc 2 §1.5)."""

    accepted: int
    pending: int
    rejected: list[Rejected]


class SyncNode(Protocol):
    """What sync needs from a node. Both Node and FakeNode have it."""

    node_id: str

    def ingest(self, record: Mapping[str, Any], from_peer: str | None = None) -> IngestResult: ...
    def version_vector(self) -> VersionVector: ...
    def records_since(self, theirs: VersionVector) -> list[Record]: ...


class PublishingNode(SyncNode, Protocol):
    def publish(
        self, *, type: str, payload: dict[str, Any], now_ms: int, identity: str = "public"
    ) -> Record: ...


# --- batching -----------------------------------------------------------------------------


def _wire_size(record: Record) -> int:
    """Bytes this record takes in a message (its canonical JSON).

    Same bytes as record.canonical_json, minus its validation: everything we send came
    out of our own store, so it was already checked on the way in.
    """
    return len(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def batch(
    records: list[Record], *, limit: int = MAX_BATCH_RECORDS, max_bytes: int = MAX_BATCH_BYTES
) -> tuple[list[Record], bool]:
    """Pick what fits in one message. Returns (batch, more).

    `records` is records_since() output: grouped by author, each author in seq order.
    We take one record per author in turn (round-robin), so one huge feed can't crowd
    out the rest, and each author's part is always a seq-ordered prefix, so a cut-off
    batch never leaves the receiver with a gap.
    """
    limit = max(0, min(limit, MAX_BATCH_RECORDS))  # the peer can ask for less, not more
    queues = [list(rs) for _, rs in groupby(records, key=lambda r: r["author"])]

    out: list[Record] = []
    size = 0
    while queues and len(out) < limit:
        for q in list(queues):  # one record from each author per pass
            if len(out) >= limit:
                break
            n = _wire_size(q[0])
            if out and size + n > max_bytes:
                return out, True  # the byte budget is full
            out.append(q.pop(0))
            size += n
            if not q:
                queues.remove(q)
    return out, bool(queues)


# --- the receiving side (called by the transport on the peer we contacted) ---------------


def _clean_vv(vv: Any) -> VersionVector:
    """A peer's version vector, keeping only well-formed entries (it's untrusted)."""
    if not isinstance(vv, dict):
        return {}
    # Fast path: every key a str and every value exactly an int (not bool), checked at
    # C speed. This is every honest peer, every time.
    if set(map(type, vv)) <= {str} and set(map(type, vv.values())) <= {int}:
        return vv
    return {
        a: n for a, n in vv.items()
        if isinstance(a, str) and isinstance(n, int) and not isinstance(n, bool)
    }


def handle_sync(node: SyncNode, req: SyncRequest) -> SyncResponse:
    """B's side of a round: send A what it lacks, plus our vv so A can push back."""
    theirs = _clean_vv(req.get("vv"))
    limit = req.get("limit", MAX_BATCH_RECORDS)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = MAX_BATCH_RECORDS
    records, more = batch(node.records_since(theirs), limit=limit)
    return {"from": node.node_id, "vv": node.version_vector(), "records": records, "more": more}


def handle_push(node: SyncNode, msg: PushRecords) -> PushResult:
    """Ingest records a peer pushed to us. Every record goes through ingest."""
    peer = msg.get("from")
    peer = peer if isinstance(peer, str) else None
    records = msg.get("records")
    if not isinstance(records, list):
        records = []
    return _ingest_all(node, records, peer)


def _ingest_all(node: SyncNode, records: list[Any], peer: str | None) -> PushResult:
    result: PushResult = {"accepted": 0, "pending": 0, "rejected": []}
    for r in records:
        outcome = node.ingest(r, peer)
        if outcome is IngestResult.ACCEPTED:
            result["accepted"] += 1
        elif outcome is IngestResult.GAP:
            result["pending"] += 1
        elif outcome is not IngestResult.DUPLICATE:  # duplicates are fine, just not news
            rid = r.get("id") if isinstance(r, dict) else None
            result["rejected"].append(
                {"id": rid if isinstance(rid, str) else "", "reason": outcome.value}
            )
    return result


# --- the initiating side ------------------------------------------------------------------


def sync_with(node: SyncNode, transport: Transport, peer: str) -> int:
    """One full round with `peer`: pull what we lack, push what it lacks.

    Returns how many records were newly chained (ours + theirs). If the peer had more
    than one batch, the rest comes next round.
    """
    req: SyncRequest = {
        "from": node.node_id,
        "vv": node.version_vector(),
        "limit": MAX_BATCH_RECORDS,
    }
    resp = transport.sync(peer, req)

    # Pull: everything goes through ingest, attributed to the peer that sent it.
    pulled = _ingest_all(node, resp.get("records") or [], peer)

    # Push: compare with the peer's vv (from before this round) and send what it lacks.
    to_push, _ = batch(node.records_since(_clean_vv(resp.get("vv"))))
    pushed = 0
    if to_push:
        pushed = transport.push(peer, {"from": node.node_id, "records": to_push})["accepted"]
    return pulled["accepted"] + pushed


class Gossiper:
    """One node's gossip loop: peer choice, rounds, push-on-publish.

    The sim makes one per node. All randomness comes from the `rng` you pass in
    (one seeded random.Random for the whole sim), so the same seed gives the same run.
    """

    def __init__(self, node: PublishingNode, transport: Transport, rng: random.Random) -> None:
        self.node = node
        self.transport = transport
        self.rng = rng
        self._round = 0
        self._last_sync: dict[str, int] = {}  # peer -> round we last synced with it

    def pick_peers(self, k: int = PEERS_PER_ROUND) -> list[str]:
        """Up to k reachable peers, least recently synced first; ties broken by rng."""
        peers = sorted(p for p in self.transport.reachable() if p != self.node.node_id)
        self.rng.shuffle(peers)  # random tie-break, deterministic for a given seed
        peers.sort(key=lambda p: self._last_sync.get(p, -1))  # stable: keeps the shuffle
        return peers[:k]

    def gossip_round(self) -> int:
        """Call once per sim tick (or every 2 s over HTTP). Returns records moved."""
        self._round += 1
        moved = 0
        for peer in self.pick_peers():
            self._last_sync[peer] = self._round
            moved += sync_with(self.node, self.transport, peer)
        return moved

    def publish(
        self, *, type: str, payload: dict[str, Any], now_ms: int, identity: str = "public"
    ) -> Record:
        """Publish through the node, then push the new record to up to 3 reachable peers.

        Peers that are missing earlier records just hold it as pending until a round
        fills the gap. It's cheap and makes new messages show up on the dashboard at once.
        """
        node = self.node
        record = node.publish(type=type, payload=payload, now_ms=now_ms, identity=identity)
        peers = sorted(p for p in self.transport.reachable() if p != node.node_id)
        for peer in self.rng.sample(peers, min(PUSH_FANOUT, len(peers))):
            self.transport.push(peer, {"from": node.node_id, "records": [record]})
        return record


# --- for tests and the 2-node harness -----------------------------------------------------


def sync_pair(a: SyncNode, b: SyncNode) -> int:
    """Sync two nodes directly (no transport) until a round moves nothing.

    Several rounds only happen when there's more than one batch. Returns the total
    records moved. Handy for tests and a tiny harness.
    """
    direct = _DirectTransport(b)
    total = 0
    while moved := sync_with(a, direct, b.node_id):
        total += moved
    return total


class _DirectTransport:
    """Calls the other node's handlers directly; always reachable."""

    def __init__(self, peer: SyncNode) -> None:
        self._peer = peer

    def reachable(self) -> list[str]:
        return [self._peer.node_id]

    def sync(self, peer: str, req: SyncRequest) -> SyncResponse:
        return handle_sync(self._peer, req)

    def push(self, peer: str, msg: PushRecords) -> PushResult:
        return handle_push(self._peer, msg)
