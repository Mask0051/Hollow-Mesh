"""P4 — payload schemas (Doc 2 §1.2): valid payloads pass, everything else is rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hollowmesh.protocol.types import PAYLOAD_SCHEMAS

ID = "ab" * 32

VALID = [
    ("profile", {"kind": "public", "level": 0}),
    ("profile", {"kind": "private", "level": 3}),
    ("post", {"text": "hi", "level": 2, "sector": "L2-S07"}),
    (
        "post",
        {"text": "x" * 280, "level": 1, "sector": "L1-S12", "tags": ["a"] * 5,
         "reply_to": ID, "expires_day": 4},
    ),
    (
        "help_request",
        {"kind": "air", "level": 3, "sector": "L3-S07", "urgency": 3, "people": 12,
         "text": "Air filter failing"},
    ),
    ("beacon", {"color": "#ff3366"}),
    ("beacon", {"color": "#00aaff", "label": "Level 3 beacon"}),
]

INVALID = [
    ("profile", {"kind": "public"}),  # missing level
    ("profile", {"kind": "secret", "level": 0}),  # unknown kind
    ("profile", {"kind": "public", "level": 4}),  # levels are 0-3
    ("profile", {"kind": "public", "level": "1"}),  # strict: no str -> int coercion
    ("profile", {"kind": "public", "level": True}),  # strict: bool isn't an int here
    ("profile", {"kind": "public", "level": 0, "extra": 1}),  # unknown field
    ("post", {"text": "x" * 281, "level": 2, "sector": "L2-S07"}),  # text > 280
    ("post", {"text": "hi", "level": 2, "sector": "level 2"}),  # sector format
    ("post", {"text": "hi", "level": 2, "sector": "L2-S07", "tags": ["a"] * 6}),  # > 5 tags
    ("post", {"text": "hi", "level": 2, "sector": "L2-S07", "reply_to": "abc"}),  # bad id
    ("help_request", {"kind": "air", "level": 3, "sector": "L3-S07", "urgency": 4,
                      "people": 1, "text": "x"}),  # urgency 1-3
    ("help_request", {"kind": "lost", "level": 3, "sector": "L3-S07", "urgency": 1,
                      "people": 1, "text": "x"}),  # unknown kind
    ("help_request", {"kind": "air", "level": 3, "sector": "L3-S07", "urgency": 1,
                      "people": -1, "text": "x"}),  # negative people
    ("beacon", {"color": "red"}),
    ("beacon", {"color": "#FF3366"}),  # lowercase only
    ("beacon", {"color": "#ff336"}),
    ("beacon", {"color": "#ff3366", "label": "x" * 41}),
]


@pytest.mark.parametrize(("type", "payload"), VALID)
def test_valid_payloads(type, payload):
    PAYLOAD_SCHEMAS[type].model_validate(payload)


@pytest.mark.parametrize(("type", "payload"), INVALID)
def test_invalid_payloads(type, payload):
    with pytest.raises(ValidationError):
        PAYLOAD_SCHEMAS[type].model_validate(payload)
