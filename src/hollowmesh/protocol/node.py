"""P3 — Node: ingest pipeline and feed storage (Doc 2 §1.3).

Node.ingest(record, from_peer):
  1. Parse + size + schema: 2 KB cap; payload validated by the type's pydantic model
     (types.py). On failure, reject with a reason.
  2. Signature: verify with the author's key. On failure, reject, write a tamper
     evidence row naming `from_peer`, give that peer a strike, fire on_event.
  3. Dedupe: id already held -> drop silently.
  4. Chain check:
     - seq == head+1 and prev == head_id -> append, then drain that author's pending.
     - seq == head+1 but prev != head_id -> suspected fork: hold in pending.
     - seq > head+1                      -> gap: hold in pending, status 'gapped',
                                            fire on_event.
     - seq <= head with a different id   -> fork (P13, stretch).

Validity must be deterministic: it depends only on the record and its chain, never
on the local clock or on which peer sent it.

Pending (gapped) records are not forwarded until the gap fills.
Storage sits behind a Store protocol: SqliteStore (HTTP) and MemStore (sim).

FakeNode (fake.py) must expose the same public API as this class.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any, Literal, TypedDict

from .record import Keypair, Record, sign_record
from .types import PAYLOAD_SCHEMAS

VersionVector = dict[str, int]  # author -> highest contiguous seq held
FeedStatus = Literal["ok", "gapped"]
Pending = dict[int, list[Record]]  # seq -> candidate records waiting for that slot


class Alert(TypedDict):
    """Alert event (Doc 2 §1.6), sent through on_event and kept as evidence.

    "gap" is our addition to the spec's fork | withhold | tamper.
    """

    t: Literal["alert"]
    kind: Literal["tamper", "gap", "fork"]
    author: str  # whose feed is affected
    peer: str | None  # who sent us the record (None = local)
    seq: int
    evidence: dict[str, Any]  # e.g. claimed_id/computed_id, have/got, both records


EventHook = Callable[[Alert], None]  # the sim / dashboard listens for alerts here


class IngestResult(Enum):
    ACCEPTED = "accepted"  # appended to the author's feed
    DUPLICATE = "duplicate"  # id already held; dropped silently
    GAP = "gap"  # held in pending until earlier seqs arrive (or prev mismatch)
    INVALID = "invalid"  # failed parse / size / schema
    TAMPERED = "tampered"  # signature failed -> tamper evidence + strike on from_peer
    FORK = "fork"  # same author+seq, different id (P13, stretch)


class Node:
    def __init__(
        self,
        public_keys: Keypair,
        *,
        private_keys: Keypair | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        """One node. Each user has a public-activity key and a private (check-in) key,
        giving two feeds that can't be linked (Doc 2 §1.1)."""
        # The node's id is its public key, so there is one source of truth (same as FakeNode)
        self.node_id = public_keys.public

        # Identities this node can publish as. "private" only exists if a key was given;
        # publish(identity="private") on a node without one raises.
        self._keys: dict[str, Keypair] = {"public": public_keys}
        if private_keys is not None:
            self._keys["private"] = private_keys

        # id -> record, for every record chained into a feed. Answers "do I have this?"
        # Pending records are NOT in here.
        self._records: dict[str, Record] = {}

        # author -> [id of seq 1, id of seq 2, ...]. The chain in order, no holes:
        # len(list) is the head seq, list[-1] is the head id.
        self._feeds: dict[str, list[str]] = {}

        # author -> {seq -> [record, ...]}. Validly signed records that can't be chained
        # yet (an earlier seq is missing). A list per seq, because two different records
        # can claim the same seq (suspected fork).
        self._pending: dict[str, Pending] = {}

        # author -> "ok" | "gapped". Lets the gap alert fire once per gap, not per record.
        # Authors not in here are "ok".
        self._status: dict[str, FeedStatus] = {}

        # Misbehaviour we've seen (tamper / gap / fork), kept after the alert fires.
        # Later served by GET /alerts.
        self._evidence: list[Alert] = []

        # peer -> number of tampered records it sent us. Counted only; nothing acts on it yet.
        self._strikes: dict[str, int] = {}

        # Alert callback (the sim / dashboard listens here). No-op if nobody listens.
        self._on_event: EventHook = on_event or (lambda event: None)


    def publish(
        self, *, type: str, payload: dict[str, Any], now_ms: int, identity: str = "public"
    ) -> Record:
        """Append a new signed record to our `identity` feed (and push it, P5).

        seq = head_seq + 1, prev = head_id, ts = max(now_ms, prev.ts).
        """
        # 1. Which key signs this? Each identity is its own feed (own author id).
        keys = self._keys.get(identity)
        if keys is None:
            raise ValueError(f"node has no {identity!r} identity")
        author = keys.public

        # 2. Build on our current head. Our own feed never has gaps, so the head is
        #    simply the last id in _feeds.
        ids = self._feeds.get(author, [])
        if not ids:
            seq, prev, ts = 1, None, now_ms  # first record in this feed
        else:
            head = self._records[ids[-1]]
            seq = len(ids) + 1
            prev = head["id"]
            ts = max(now_ms, head["ts"])  # ts never goes backwards within a feed

        # 3. The genesis record (seq 1) must be a profile (Doc 2 §1.1). Peers reject
        #    anything else at seq 1, so refuse it here instead of making a dead feed.
        if seq == 1 and type != "profile":
            raise ValueError(f"first record of a feed must be 'profile', got {type!r}")

        # 4. Check the payload against its schema now, so we never sign something our
        #    own peers would reject. (pydantic's ValidationError is a ValueError.)
        schema = PAYLOAD_SCHEMAS.get(type)
        if schema is None:
            raise ValueError(f"unknown record type {type!r}")
        schema.model_validate(payload)

        # 5. Build, hash and sign. sign_record checks seq/prev/ts and the 2 KB cap.
        record = sign_record(keys, seq=seq, prev=prev, type=type, payload=payload, ts=ts)

        # 6. Store it through ingest, the same path as everyone else's records, so our
        #    own records get the exact checks our peers will apply.
        result = self.ingest(record)
        if result is not IngestResult.ACCEPTED:
            raise RuntimeError(f"own record rejected by ingest: {result}")  # a bug if hit

        # 7. P5 adds: push to up to 3 reachable peers.
        return record

    def ingest(self, record: Mapping[str, Any], from_peer: str | None = None) -> IngestResult:
        """Take a record from a peer. Never raises on bad input.

        `record` is untrusted (a Mapping, not a Record) until steps 1-2 pass.
        """
        # --- 1. Shape, size, schema -> INVALID (no strike, no alert) ---------------------
        # 1a. (Decision 1) Ask record.py WHY a record fails, not just whether:
        #     check_record(record) -> None | "malformed" | "bad_id" | "bad_sig".
        #     (verify_record can become `check_record(r) is None`.)
        # 1b. "malformed" -> return INVALID.
        # 1c. (Decision 2) type not in PAYLOAD_SCHEMAS -> return INVALID.
        # 1d. Validate payload with PAYLOAD_SCHEMAS[type]; pydantic ValidationError -> INVALID.
        # 1e. (Decision 3) seq == 1 and type != "profile" -> INVALID.
        #
        # --- 2. id + signature -> TAMPERED -------------------------------------------------
        # 2a. "bad_id" or "bad_sig" -> tampered. Must run BEFORE dedupe: a relay can alter
        #     the payload but keep the original id field.
        # 2b. Append evidence: kind "tamper", author, peer=from_peer, seq, claimed_id,
        #     computed_id (sha256 of the signing bytes).
        # 2c. If from_peer: _strikes[from_peer] += 1.
        # 2d. self._alert("tamper", ...). Store nothing. Return TAMPERED.
        #
        # --- 3. Dedupe -> DUPLICATE ---------------------------------------------------------
        # 3a. id in _records -> DUPLICATE.
        # 3b. id already in this author's _pending (any seq) -> DUPLICATE.
        #
        # --- 4. Chain check -----------------------------------------------------------------
        # 4a. head = len(_feeds[author]) (0 if none); head_id = last id or None.
        # 4b. seq <= head (different id, since dedupe passed) -> fork.
        #     (Decision 4) Evidence with both records (_records[_feeds[author][seq-1]] and
        #     this one), self._alert("fork", ...), return FORK. No feed freeze (P13).
        # 4c. seq == head + 1 and prev == head_id -> self._append(record),
        #     self._drain_pending(author), return ACCEPTED.
        # 4d. seq == head + 1 and prev != head_id -> suspected fork: add to _pending,
        #     return GAP. (It only drains if the chain later matches.)
        # 4e. seq > head + 1 -> gap:
        #     - add to _pending (if this author already has 1000 pending, drop it instead);
        #     - if _status[author] != "gapped": set it, add a "gap" evidence row,
        #       self._alert("gap", ..., seq=head + 1, evidence {"have": head, "got": seq});
        #     - return GAP.
        raise NotImplementedError("P3")

    def version_vector(self) -> VersionVector:
        # 1. {author: len(ids) for author, ids in _feeds.items()}. Pending never counts.
        # 2. Return a new dict, so callers can't modify our state.
        raise NotImplementedError("P3")

    def records_since(self, theirs: VersionVector) -> list[Record]:
        """Everything we hold that a peer with `theirs` is missing."""
        # 1. For each author in sorted(_feeds) (sorted = deterministic order):
        # 2.   start = theirs.get(author, 0)
        # 3.   add records for _feeds[author][start:] in order (index start = seq start+1).
        # 4. Pending records are never sent. (P5 adds the 500-record / round-robin limit.)
        # Same output as FakeNode.records_since for the same holdings.
        raise NotImplementedError("P5")

    def all_records(self) -> list[Record]:
        # 1. Every record in _records (pending excluded), sorted by (author, seq).
        raise NotImplementedError("P3")

    # --- helpers --------------------------------------------------------------------------

    def _append(self, record: Record) -> None:
        # 1. Assumes the chain check passed (seq == head+1, prev == head_id).
        # 2. _records[id] = record; _feeds[author].append(id) (create the list if new).
        raise NotImplementedError("P3")

    def _drain_pending(self, author: str) -> None:
        # Loop:
        # 1. head = len(_feeds[author]); head_id = _feeds[author][-1].
        # 2. Candidates = _pending[author].get(head + 1, []).
        # 3. Find the candidate whose prev == head_id. None -> stop.
        # 4. Remove it from _pending (drop the seq key if its list is now empty),
        #    self._append(it), and loop again.
        # After the loop: if _pending[author] is empty, set _status[author] = "ok".
        # (Optional: fire a "gap resolved" event for the dashboard.)
        raise NotImplementedError("P3")

    def _alert(self, kind: Literal["tamper", "gap", "fork"], *, author: str,
               peer: str | None, seq: int, evidence: dict[str, Any]) -> None:
        # 1. Build an Alert: {"t": "alert", "kind": kind, "author": author, "peer": peer,
        #                     "seq": seq, "evidence": evidence}.
        # 2. Keep it: self._evidence.append(alert).
        # 3. self._on_event(alert).
        raise NotImplementedError("P3")
