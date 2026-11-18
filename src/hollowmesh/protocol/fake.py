"""P2 — FakeNode: same API as Node (NodeAPI), no verification, no chain checks.

Lets the teammate build the sim (H4) before the real protocol works. Records are
really signed (so they're valid later), but ingest trusts them: no signature check,
no prev check, no alerts. Gaps are still tracked so version vectors behave like the
real node's and the sim's sync loop doesn't change when Node swaps in (P6).
"""

from __future__ import annotations

from typing import Any

from .node import EventHook, IngestResult, VersionVector
from .record import Keypair, sign_record


class FakeNode:
    def __init__(
        self,
        public_keys: Keypair,
        *,
        private_keys: Keypair | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        # The node's id is its public key, so there is one source of truth
        self.node_id = public_keys.public

        # Identities this node can publish as; "private" only if one was given
        self._keys: dict[str, Keypair] = {"public": public_keys}
        if private_keys is not None:
            self._keys["private"] = private_keys

        # Every record this node holds (its own and received): id -> record
        self._records: dict[str, dict[str, Any]] = {}

        # Per author: seq -> id, for every seq held (including ones past a gap)
        self._seqs: dict[str, dict[int, str]] = {}

        # Per author: highest contiguous seq held (1..n all present)
        self._vv: VersionVector = {}

        # Latest record per author: author -> (seq, id, ts); publish builds on this
        self._heads: dict[str, tuple[int, str, int]] = {}

        # Alert hook (gap/tamper/fork); FakeNode never fires it, the real node will
        self._on_event: EventHook = on_event or (lambda event: None)

    def publish(self, *, type: str, payload: dict[str, Any], now_ms: int,
                identity: str = "public") -> dict[str, Any]:
        """Append a new signed record to one of this node's own feeds."""

        keys = self._keys.get(identity)
        if keys is None:
            raise ValueError(f"node has no {identity!r} identity")
        author = keys.public

        head = self._heads.get(author)
        if head is None:
            # first record in this feed
            seq, prev, ts = 1, None, now_ms
        else:
            head_seq, head_id, head_ts = head
            seq = head_seq + 1
            prev = head_id
            ts = max(now_ms, head_ts)

        record = sign_record(keys, seq=seq, prev=prev, type=type, payload=payload, ts=ts)
        self._store(record)
        return record

    def ingest(self, record: dict[str, Any], from_peer: str | None = None) -> IngestResult:
        """Store a peer's record. Trusts it: only dedupe and gap bookkeeping.

        `from_peer` is unused here; the real node uses it for tamper evidence + strikes.
        """
        rid = record.get("id") if isinstance(record, dict) else None
        if not isinstance(rid, str):
            # Not even shaped like a record; the real node checks properly
            return IngestResult.INVALID
        if rid in self._records:
            return IngestResult.DUPLICATE

        self._store(record)
        # Past the contiguous prefix -> the real node would hold it in `pending`
        if record["seq"] > self._vv.get(record["author"], 0):
            return IngestResult.GAP
        return IngestResult.ACCEPTED

    def version_vector(self) -> VersionVector:
        return dict(self._vv)

    def records_since(self, theirs: VersionVector) -> list[dict[str, Any]]:
        """What a peer with `theirs` lacks: seqs theirs[a]+1 .. ours[a], per author.

        Like the real node, records past a gap are not forwarded until it fills.
        """
        out: list[dict[str, Any]] = []
        for author in sorted(self._vv):
            seqs = self._seqs[author]
            for seq in range(theirs.get(author, 0) + 1, self._vv[author] + 1):
                out.append(self._records[seqs[seq]])
        return out

    def all_records(self) -> list[dict[str, Any]]:
        return sorted(self._records.values(), key=lambda r: (r["author"], r["seq"]))

    def _store(self, record: dict[str, Any]) -> None:
        """Index a record and advance the author's contiguous seq and head."""
        author, seq = record["author"], record["seq"]
        self._records[record["id"]] = record

        seqs = self._seqs.setdefault(author, {})
        seqs.setdefault(seq, record["id"])  # first one wins; forks are the real node's job

        n = self._vv.get(author, 0)
        while n + 1 in seqs:
            n += 1
        if n:
            self._vv[author] = n

        head = self._heads.get(author)
        if head is None or seq > head[0]:
            self._heads[author] = (seq, record["id"], record["ts"])
