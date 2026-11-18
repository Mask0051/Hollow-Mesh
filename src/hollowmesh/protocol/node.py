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
from typing import Any, Literal, TypedDict, cast

from .record import Keypair, Record, check_record, record_id, sign_record
from .types import PAYLOAD_SCHEMAS

VersionVector = dict[str, int]  # author -> highest contiguous seq held
FeedStatus = Literal["ok", "gapped"]
Pending = dict[int, list[Record]]  # seq -> candidate records waiting for that slot
MAX_PENDING_PER_AUTHOR = 1000  # out-of-order buffer cap (Doc 2 §1.3)


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
            raise RuntimeError(f"own record rejected by ingest (bug?): {result}")  # a bug if hit

        # 7. P5 adds: push to up to 3 reachable peers.
        return record

    def ingest(self, record: Mapping[str, Any], from_peer: str | None = None) -> IngestResult:
        """Take a record from a peer. Never raises on bad input.

        `record` is untrusted (a Mapping, not a Record) until steps 1-2 pass.
        """
        # --- 1. Envelope, id, signature (record.py does the crypto) -------------------------
        problem = check_record(record)
        if problem == "malformed":
            # Not even a well-formed record: wrong fields, bad hex, too big, floats...
            return IngestResult.INVALID

        if problem is not None:
            # --- 2. "bad_id" / "bad_sig" -> TAMPERED -----------------------------------------
            # Well-formed but altered: the content doesn't hash to `id`, or the author
            # didn't sign it. This runs BEFORE dedupe on purpose: a relay can alter the
            # payload but keep the original id, and dedupe would wave it through.
            author, seq = record["author"], record["seq"]
            if from_peer is not None:
                self._strikes[from_peer] = self._strikes.get(from_peer, 0) + 1
            
            self._alert(
                "tamper",
                author=author,
                peer=from_peer,
                seq=seq,
                evidence={
                    "problem": problem,
                    "claimed_id": record["id"],  # what the record says it is
                    "computed_id": record_id(record),  # what its content hashes to
                    "record": dict(record),
                },
            )
            return IngestResult.TAMPERED

        # From here on the record is authentic: the author really signed exactly this.
        rec = cast(Record, record)
        author, seq, rid = rec["author"], rec["seq"], rec["id"]

        # --- 1 (cont). Payload schema -> INVALID (no strike: the author signed bad data) ---
        # Checked after the signature, so a relay that rewrites a payload into something
        # invalid is still caught as tampering above, not quietly called INVALID.
        schema = PAYLOAD_SCHEMAS.get(rec["type"])
        if schema is None:
            return IngestResult.INVALID  # unknown type (Decision 2)
        try:
            schema.model_validate(rec["payload"])
        except ValueError:  # pydantic's ValidationError is a ValueError
            return IngestResult.INVALID
        if seq == 1 and rec["type"] != "profile":
            return IngestResult.INVALID  # genesis must be a profile (Decision 3)

        # --- 3. Dedupe -> DUPLICATE -----------------------------------------------------------
        if rid in self._records or self._is_pending(author, rid):
            return IngestResult.DUPLICATE

        # --- 4. Chain check ---------------------------------------------------------------
        ids = self._feeds.get(author, [])
        head = len(ids)  # highest seq we've chained
        head_id = ids[-1] if ids else None

        if seq <= head:
            # 4b. We already chained a different record at this seq: the author signed
            #     two versions of the same slot. Both signatures are the proof (Decision 4).
            self._alert(
                "fork",
                author=author,
                peer=from_peer,
                seq=seq,
                evidence={"ours": dict(self._records[ids[seq - 1]]), "theirs": dict(rec)},
            )
            return IngestResult.FORK

        if seq == head + 1 and rec["prev"] == head_id:
            # 4c. The normal case: it extends the chain. Then pull in anything that was
            #     waiting on it.
            self._append(rec)
            self._drain_pending(author)
            return IngestResult.ACCEPTED

        # 4d / 4e. Can't chain it yet: hold it in pending.
        pending = self._pending.setdefault(author, {})
        if sum(len(c) for c in pending.values()) >= MAX_PENDING_PER_AUTHOR:
            return IngestResult.GAP  # buffer full: drop it; a later sync will resend it
        pending.setdefault(seq, []).append(rec)

        if seq > head + 1 and self._status.get(author, "ok") != "gapped":
            # 4e. First record past a hole: something between head+1 and seq-1 is
            #     missing (dropped by a relay, or just not here yet). Alert once per gap.
            self._status[author] = "gapped"
            self._alert(
                "gap",
                author=author,
                peer=from_peer,
                seq=head + 1,  # the first missing seq
                evidence={"have": head, "got": seq},
            )
        # 4d (seq == head+1 but prev != head_id) is a suspected fork: it waits silently,
        # and _drain_pending reports it as a fork if the real chain passes it by.
        return IngestResult.GAP

    def version_vector(self) -> VersionVector:
        # Our feeds never have holes, so each author's count IS the highest seq held.
        # Pending never counts. A new dict, so callers can't modify our state.
        return {author: len(ids) for author, ids in self._feeds.items()}

    def records_since(self, theirs: VersionVector) -> list[Record]:
        """Everything we hold that a peer with `theirs` is missing."""
        out: list[Record] = []
        for author in sorted(self._feeds):  # sorted = same order on every node
            # They hold seqs 1..start, so they need seq start+1 onward = ids[start:].
            # max(0, ...) so a bogus negative count can't slice from the end.
            start = max(0, theirs.get(author, 0))
            out.extend(self._records[rid] for rid in self._feeds[author][start:])
        # Pending records are never sent. (P5 adds the 500-record / round-robin limit.)
        return out

    def all_records(self) -> list[Record]:
        # Every chained record (pending excluded), by (author, seq). _feeds is already in
        # seq order, so walking it in sorted-author order needs no extra sort.
        return [self._records[rid] for author in sorted(self._feeds) for rid in self._feeds[author]]

    # --- helpers --------------------------------------------------------------------------

    def _append(self, record: Record) -> None:
        # Only called after the chain check passed (seq == head+1, prev == head_id),
        # so appending keeps the list hole-free: index seq-1 holds seq.
        self._records[record["id"]] = record
        self._feeds.setdefault(record["author"], []).append(record["id"])

    def _is_pending(self, author: str, rid: str) -> bool:
        # At most MAX_PENDING_PER_AUTHOR records to scan, so a scan is fine.
        return any(
            r["id"] == rid for cands in self._pending.get(author, {}).values() for r in cands
        )

    def _drain_pending(self, author: str) -> None:
        """After an append, chain any waiting records that now fit, one after another."""
        pending = self._pending.get(author)
        if not pending:
            return

        ids = self._feeds[author]
        while True:
            head = len(ids)
            # The waiting record for the next slot, if one of them links to our head.
            nxt = next((r for r in pending.get(head + 1, []) if r["prev"] == ids[-1]), None)
            if nxt is None:
                break
            pending[head + 1].remove(nxt)
            self._append(nxt)

        # Anything left at a seq we've now passed lost to the real chain: the author
        # signed two records for that slot. Report it as a fork and drop it.
        head = len(ids)
        for seq in sorted(s for s in pending if s <= head):
            for loser in pending.pop(seq):
                self._alert(
                    "fork",
                    author=author,
                    peer=None,
                    seq=seq,
                    evidence={"ours": dict(self._records[ids[seq - 1]]), "theirs": dict(loser)},
                )

        # Tidy up empty slots; no pending left means no gap left.
        for seq in [s for s, cands in pending.items() if not cands]:
            del pending[seq]
        if not pending:
            del self._pending[author]
            self._status[author] = "ok"

    def _alert(self, kind: Literal["tamper", "gap", "fork"], *, author: str,
            peer: str | None, seq: int, evidence: dict[str, Any]) -> None:
        # One place for all alerts: keep it as evidence (for GET /alerts later), then
        # tell whoever is listening (the sim / dashboard).
        alert: Alert = {
            "t": "alert",
            "kind": kind,
            "author": author,
            "peer": peer,
            "seq": seq,
            "evidence": evidence,
        }
        self._evidence.append(alert)
        self._on_event(alert)
