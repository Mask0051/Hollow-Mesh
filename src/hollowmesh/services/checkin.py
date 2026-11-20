"""Family check-ins (prompt #9: families split across levels during a blackout).

A family shares one 32-byte key (the sim hands them out; on laptops it would be an
invite string). A check-in is a CheckinBody, sealed with that key (XSalsa20-Poly1305,
PyNaCl SecretBox) and published from the member's PRIVATE feed, so:
  * relays carry it like any record, but only family can read it;
  * nothing links it to the member's public feed (health reports, posts);
  * there is no group tag: a phone simply tries each family key it holds.

Latest check-in per person replaces older ones (Doc 2 §1.2 lifetimes).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Sequence
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from pydantic import ValidationError

from ..protocol.types import CheckinBody

KEY_BYTES = SecretBox.KEY_SIZE  # 32
NONCE_BYTES = SecretBox.NONCE_SIZE  # 24


def seal(key: bytes, body: dict[str, Any], nonce: bytes) -> dict[str, str]:
    """Encrypt a check-in body into a `checkin` payload. Raises if the body is invalid.

    `nonce` must be 24 fresh random bytes (the sim passes seeded ones, for determinism).
    """
    clean = CheckinBody.model_validate(body).model_dump()
    pt = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()
    msg = SecretBox(key).encrypt(pt, nonce)
    return {"nonce": base64.b64encode(msg.nonce).decode(),
            "ct": base64.b64encode(msg.ciphertext).decode()}


def open_checkin(keys: Sequence[bytes], payload: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """(index of the key that opened it, body) or None: not ours, or junk. Never raises."""
    try:
        nonce = base64.b64decode(payload["nonce"], validate=True)
        ct = base64.b64decode(payload["ct"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    for i, key in enumerate(keys):
        try:
            pt = SecretBox(key).decrypt(ct, nonce)
        except (CryptoError, ValueError, TypeError):
            continue
        try:  # a family member could still send garbage: check it like any payload
            return i, CheckinBody.model_validate(json.loads(pt)).model_dump()
        except (ValueError, ValidationError):
            return None
    return None


def family_view(records: Iterable[dict[str, Any]], keys: Sequence[bytes]
                ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int]:
    """Check-ins a phone can read, newest first, as (record, body); plus how many sealed
    check-ins it carries that it can NOT read (other families')."""
    mine, sealed = [], 0
    for x in records:
        if x["type"] != "checkin":
            continue
        opened = open_checkin(keys, x["payload"])
        if opened is None:
            sealed += 1
        else:
            mine.append((x, opened[1]))
    mine.sort(key=lambda xb: (xb[0]["ts"], xb[0]["author"], xb[0]["seq"]), reverse=True)
    return mine, sealed
