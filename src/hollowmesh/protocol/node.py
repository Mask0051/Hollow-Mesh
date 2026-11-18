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

from collections.abc import Callable
from enum import Enum
from typing import Any

from .record import Keypair

VersionVector = dict[str, int]  # author -> highest contiguous seq held
EventHook = Callable[[dict[str, Any]], None]  # gets alert events (Doc 2 §1.6 "alert")


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
        # 1. node_id = public_keys.public (same as FakeNode).
        self.node_id = public_keys.public
        # 2. _keys: {"public": public_keys}, plus "private" only if private_keys was given.
        self._keys: dict[str, Keypair] = {}
        # 3. _records: id -> record. Only records that are chained into a feed live here.
        self._records : dict[str, dict[str, Any]] = {}
        # 4. _feeds: author -> list of ids, index = seq - 1. len(list) is the head seq,
        #    list[-1] is the head id.
        self.
        # 5. _pending: author -> {seq -> [record, ...]}. Validly signed records that can't be
        #    appended yet. A list per seq because two different records can claim one seq.
        # 6. _status: author -> "ok" | "gapped". Lets the gap alert fire once, not per record.
        # 7. _evidence: list of dicts (kind, author, peer, seq, ...). Later served by /alerts.
        # 8. _strikes: peer -> int. Counted only; nothing acts on it yet.
        # 9. _on_event: on_event, or a no-op lambda if None.
        raise NotImplementedError("P3")

    def publish(
        self, *, type: str, payload: dict[str, Any], now_ms: int, identity: str = "public"
    ) -> dict[str, Any]:
        """Append a new signed record to our `identity` feed (and push it, P5).

        seq = head_seq + 1, prev = head_id, ts = max(now_ms, prev.ts).
        """
        # 1. Look up keys for `identity`; ValueError if this node doesn't have it.
        # 2. Read this author's head from _feeds: seq, id, and the head record's ts.
        #    Empty feed -> seq=1, prev=None, ts=now_ms.
        #    Otherwise -> seq=head+1, prev=head_id, ts=max(now_ms, head_ts).
        # 3. (Decision 3) seq == 1 must be type "profile"; raise ValueError otherwise.
        # 4. Validate payload against PAYLOAD_SCHEMAS[type]; raise ValueError if bad, so an
        #    author can never publish something its own peers would reject.
        # 5. sign_record(keys, seq=..., prev=..., type=..., payload=..., ts=...).
        # 6. (Decision 6) result = self.ingest(record): one code path for all records.
        #    Anything other than ACCEPTED here is a bug -> raise/assert.
        # 7. Return the record. (P5 adds: push to up to 3 reachable peers.)
        raise NotImplementedError("P3")

    def ingest(self, record: dict[str, Any], from_peer: str | None = None) -> IngestResult:
        """Take a record from a peer. Never raises on bad input."""
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

    def records_since(self, theirs: VersionVector) -> list[dict[str, Any]]:
        """Everything we hold that a peer with `theirs` is missing."""
        # 1. For each author in sorted(_feeds) (sorted = deterministic order):
        # 2.   start = theirs.get(author, 0)
        # 3.   add records for _feeds[author][start:] in order (index start = seq start+1).
        # 4. Pending records are never sent. (P5 adds the 500-record / round-robin limit.)
        # Same output as FakeNode.records_since for the same holdings.
        raise NotImplementedError("P5")

    def all_records(self) -> list[dict[str, Any]]:
        # 1. Every record in _records (pending excluded), sorted by (author, seq).
        raise NotImplementedError("P3")

    # --- helpers --------------------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
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

    def _alert(self, kind: str, *, author: str, peer: str | None, seq: int,
               evidence: dict[str, Any]) -> None:
        # 1. Build {"t": "alert", "kind": kind, "author": author, "peer": peer,
        #           "seq": seq, "evidence": evidence}   (Doc 2 §1.6; "gap" is a new kind).
        # 2. self._on_event(event).
        raise NotImplementedError("P3")
