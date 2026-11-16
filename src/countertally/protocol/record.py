"""M1 — Record format: canonical JSON, sha256 id, ed25519 sign/verify.

A record is one entry in an author's append-only feed. Each record links to the
previous one by hash (`prev`), so a feed is a hash chain signed by its author.

Write the tests for canonical serialization and tamper detection yourself first (Doc 4 §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Keypair:
    """An ed25519 identity. `public` (hex) is the author id."""

    secret: bytes
    public: str

    @classmethod
    def generate(cls) -> Keypair:
        raise NotImplementedError("M1")

    @classmethod
    def from_seed(cls, seed: bytes) -> Keypair:
        """Deterministic keys, so the sim can reproduce a run from one seed."""
        raise NotImplementedError("M1")


def canonical_json(obj: Any) -> bytes:
    """Serialize deterministically: sorted keys, no whitespace, UTF-8."""
    raise NotImplementedError("M1")


def record_id(unsigned: dict[str, Any]) -> str:
    """sha256 hex of canonical_json(the record without `id` and `sig`)."""
    raise NotImplementedError("M1")


def sign_record(
    keys: Keypair, *, seq: int, prev: str | None, type: str, body: dict[str, Any], day: int
) -> dict[str, Any]:
    """Build, hash and sign a record. Returns the full record with `id` and `sig`."""
    raise NotImplementedError("M1")


def verify_record(record: dict[str, Any]) -> bool:
    """True if `id` matches the content and `sig` is a valid signature by `author`."""
    raise NotImplementedError("M1")
