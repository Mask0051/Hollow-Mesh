"""M3 — Node: ingest pipeline and feed storage.

ingest: parse -> verify signature -> dedupe -> chain check (seq/prev) -> append.
Detects gaps (missing seqs) and tampering/forks (same author+seq, different id).

FakeNode (fake.py) must expose the same public API as this class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .record import Keypair

VersionVector = dict[str, int]  # author -> highest contiguous seq held


class IngestResult(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    GAP = "gap"  # buffered until earlier seqs arrive
    BAD_SIGNATURE = "bad_signature"
    TAMPERED = "tampered"  # chain/hash mismatch
    FORK = "fork"  # M8


@dataclass
class Node:
    node_id: str
    keys: Keypair
    feeds: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def publish(self, type: str, body: dict[str, Any], day: int) -> dict[str, Any]:
        """Append a new signed record to our own feed (and push it, M4)."""
        raise NotImplementedError("M3")

    def ingest(self, record: dict[str, Any]) -> IngestResult:
        raise NotImplementedError("M3")

    def version_vector(self) -> VersionVector:
        raise NotImplementedError("M3")

    def records_since(self, theirs: VersionVector) -> list[dict[str, Any]]:
        """Everything we hold that a peer with `theirs` is missing."""
        raise NotImplementedError("M4")

    def all_records(self) -> list[dict[str, Any]]:
        raise NotImplementedError("M3")
