"""P1 — Record format: canonical JSON, sha256 id, ed25519 sign/verify.

A record is one entry in an author's append-only feed. Each record links to the
previous one by hash (`prev`), so a feed is a hash chain signed by its author.

Write the tests for canonical serialization and tamper detection yourself first (Doc 4 §2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

import nacl.utils
from nacl.exceptions import CryptoError
from nacl.signing import SigningKey, VerifyKey

FORMAT_VERSION = 1
MAX_RECORD_BYTES = 2048
# Largest int a JS number (IEEE-754 double) holds exactly; beyond this, JS parsers
# round the value, re-serialize different bytes, and the id/sig no longer match.
MAX_SAFE_INT = 2**53 - 1
_HEX = set("0123456789abcdef")
_RECORD_KEYS = {"v", "author", "seq", "prev", "type", "ts", "payload", "id", "sig"}


class Record(TypedDict):
    """A signed record (Doc 2 §1.1). At runtime it's a plain dict: same JSON, same bytes.

    Use it for records we produced or verified. Input from peers is untrusted and
    stays `Mapping[str, Any]` until it's checked.
    """

    v: int  # format version
    author: str  # ed25519 public key, 64 hex
    seq: int  # 1-based, +1 per record, per author
    prev: str | None  # id of record seq-1; None only when seq == 1
    type: str  # record type, e.g. "post"
    ts: int  # author's claimed clock, int ms (informational only)
    payload: dict[str, Any]  # type-specific, validated by types.PAYLOAD_SCHEMAS
    id: str  # sha256 hex of the signing bytes
    sig: str  # ed25519 signature over the signing bytes, 128 hex


@dataclass(frozen=True)
class Keypair:
    """An ed25519 identity. `public` (hex) is the author id."""
    
    secret: bytes = field(repr=False)
    public: str

    @classmethod
    def generate(cls) -> Keypair:
        """Randomly generated keys"""
        return cls.from_seed(seed=nacl.utils.random(32))

    @classmethod
    def from_seed(cls, seed: bytes) -> Keypair:
        """Deterministic keys, so the sim can reproduce a run from one seed."""
        sk = SigningKey(seed=seed)

        public = bytes(sk.verify_key).hex()
        secret = bytes(sk)
        
        return cls(secret=secret, public=public)


def _check_canonical(x: Any, path: str = "$") -> None:
    """Reject anything that could serialize differently across machines/languages."""
    if isinstance(x, float):
        raise ValueError(f"float not allowed at {path}: {x!r}")
    if isinstance(x, int) and not -MAX_SAFE_INT <= x <= MAX_SAFE_INT:
        raise ValueError(f"int outside ±(2**53 - 1) at {path}: {x!r}")
    if isinstance(x, dict):
        for k, v in x.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string key at {path}: {k!r}")
            _check_canonical(v, f"{path}.{k}")
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            _check_canonical(v, f"{path}[{i}]")


def canonical_json(obj: Any) -> bytes:
    """Serialize deterministically: sorted keys, no whitespace, UTF-8, no floats."""
    _check_canonical(obj)
    # sort_keys=True: sorted keys, nested too
    # separators=(",", ":"): no whitespace
    # ensure_ascii=False + .encode("utf-8"): real UTF-8, not \u escapes
    # allow_nan=False: backup guard against NaN/Infinity
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")

def record_id(unsigned: Mapping[str, Any]) -> str:
    """sha256 hex of canonical_json(the record without `id` and `sig`)."""
    #remove id and sig if already in json
    unsigned_trimmed = {k: v for k,v in unsigned.items() if k not in ("id", "sig")}
    canonical_unsigned = canonical_json(unsigned_trimmed)
    hash_ob = hashlib.sha256(canonical_unsigned)
    return hash_ob.hexdigest()

def _is_hex(s: Any, length: int) -> bool:
    return isinstance(s, str) and len(s) == length and set(s) <= _HEX


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)

def _check_fields(*, seq: Any, prev: Any, type: Any, ts: Any, payload: Any) -> None:
    """Stateless field checks shared by sign_record and verify_record."""
    # stateless chain check (the node picks seq/prev from the author's head)
    if not _is_int(seq) or seq < 1:
        raise ValueError(f"seq must be an int >= 1, got {seq!r}")
    if seq == 1:
        if prev is not None:
            raise ValueError(f"prev must be None when seq == 1, got {prev!r}")
    elif not _is_hex(prev, 64):
        raise ValueError(f"prev must be 64 lowercase hex when seq > 1, got {prev!r}")
    if not isinstance(type, str) or not type:
        raise ValueError("type must be a non-empty string")
    if not _is_int(ts) or ts < 0:
        raise ValueError(f"ts must be a non-negative int (ms), got {ts!r}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")


def sign_record(
    keys: Keypair, *, seq: int, prev: str | None, type: str, payload: dict[str, Any], ts: int
) -> Record:
    """Build, hash and sign a record. Returns the full record with `id` and `sig`."""

    #1. field checks
    _check_fields(seq=seq, prev=prev, type=type, ts=ts, payload=payload)

    #2. Unsigned envelope layout (before id/sig)
    unsigned = {
        "v" : FORMAT_VERSION,
        "author": keys.public,
        "seq": seq,
        "prev": prev,
        "type": type,
        "ts": ts,
        "payload": payload,
    }

    # 3. Serialize once; these exact bytes are both hashed and signed
    signing_bytes = canonical_json(unsigned)
    if len(signing_bytes) > MAX_RECORD_BYTES:
        raise ValueError(f"record is {len(signing_bytes)} bytes, cap is {MAX_RECORD_BYTES}")

    #4. id = sha256 of signing bytes
    rid = hashlib.sha256(signing_bytes).hexdigest()

    # 5. Signature over the same bytes (.signature = just the 64-byte sig, not sig+message)
    sig = SigningKey(keys.secret).sign(signing_bytes).signature.hex()
    return cast(Record, {**unsigned, "id": rid, "sig": sig})


def verify_record(record: Mapping[str, Any]) -> bool:
    """True if `id` matches the content and `sig` is a valid signature by `author`.

    Records arrive from untrusted peers, so this never raises: anything malformed is False.
    """
    try:
        #1. exact envelope: no missing or extra fields, known version
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            return False
        if record["v"] != FORMAT_VERSION or isinstance(record["v"], bool):
            return False
        if not (
            _is_hex(record["author"], 64)
            and _is_hex(record["id"], 64)
            and _is_hex(record["sig"], 128)
        ):
            return False
        _check_fields(
            seq=record["seq"],
            prev=record["prev"],
            type=record["type"],
            ts=record["ts"],
            payload=record["payload"],
        )

        #2. rebuild the exact signing bytes sign_record produced
        unsigned = {k: v for k, v in record.items() if k not in ("id", "sig")}
        signing_bytes = canonical_json(unsigned)
        if len(signing_bytes) > MAX_RECORD_BYTES:
            return False

        #3. id must be the hash of those bytes
        if hashlib.sha256(signing_bytes).hexdigest() != record["id"]:
            return False

        #4. sig must be the author's signature over those same bytes
        VerifyKey(bytes.fromhex(record["author"])).verify(
            signing_bytes, bytes.fromhex(record["sig"])
        )
        return True
    except (ValueError, TypeError,RecursionError, CryptoError):
        # ValueError: _check_fields / canonical_json; TypeError: unserializable payload;
        # CryptoError: bad signature (BadSignatureError) or an invalid public key
        return False
