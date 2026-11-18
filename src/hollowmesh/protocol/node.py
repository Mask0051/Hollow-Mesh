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
        raise NotImplementedError("P3")

    def publish(
        self, *, type: str, payload: dict[str, Any], now_ms: int, identity: str = "public"
    ) -> dict[str, Any]:
        """Append a new signed record to our `identity` feed (and push it, P5).

        seq = head_seq + 1, prev = head_id, ts = max(now_ms, prev.ts).
        """
        raise NotImplementedError("P3")

    def ingest(self, record: dict[str, Any], from_peer: str | None = None) -> IngestResult:
        raise NotImplementedError("P3")

    def version_vector(self) -> VersionVector:
        raise NotImplementedError("P3")

    def records_since(self, theirs: VersionVector) -> list[dict[str, Any]]:
        """Everything we hold that a peer with `theirs` is missing."""
        raise NotImplementedError("P5")

    def all_records(self) -> list[dict[str, Any]]:
        raise NotImplementedError("P3")
