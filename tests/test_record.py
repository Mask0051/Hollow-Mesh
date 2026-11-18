from typing import Any

import pytest
from nacl.signing import VerifyKey

from hollowmesh.protocol.record import (
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    MAX_SAFE_INT,
    Keypair,
    canonical_json,
    record_id,
    sign_record,
    verify_record,
)

SEED = b"\x01" * 32
PREV = "ab" * 32  # any valid 64-char lowercase hex


@pytest.fixture
def keys() -> Keypair:
    return Keypair.from_seed(SEED)


def make(keys: Keypair, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(seq=1, prev=None, type="note", payload={"msg": "hi"}, ts=1_000)
    args.update(overrides)
    return sign_record(keys, **args)


# --- Keypair ---------------------------------------------------------------


def test_from_seed_is_deterministic():
    a = Keypair.from_seed(SEED)
    b = Keypair.from_seed(SEED)
    assert a == b
    assert len(a.public) == 64  # 32-byte ed25519 public key, hex


def test_different_seeds_give_different_keys():
    assert Keypair.from_seed(b"\x01" * 32).public != Keypair.from_seed(b"\x02" * 32).public


def test_generate_is_random():
    assert Keypair.generate().public != Keypair.generate().public


def test_repr_hides_secret():
    assert "secret" not in repr(Keypair.from_seed(SEED))


# --- canonical_json --------------------------------------------------------


def test_canonical_golden_bytes():
    # Exact bytes are the contract: any other implementation must produce these.
    obj = {"b": 1, "a": {"d": [1, 2], "c": None}, "e": True, "f": "é"}
    assert canonical_json(obj) == b'{"a":{"c":null,"d":[1,2]},"b":1,"e":true,"f":"\xc3\xa9"}'


def test_canonical_ignores_insertion_order():
    assert canonical_json({"a": 1, "b": {"x": 1, "y": 2}}) == canonical_json(
        {"b": {"y": 2, "x": 1}, "a": 1}
    )


def test_canonical_tuple_same_as_list():
    assert canonical_json({"a": (1, 2)}) == canonical_json({"a": [1, 2]})


@pytest.mark.parametrize(
    "bad",
    [
        1.5,
        {"a": 1.0},
        {"a": [1, {"b": float("nan")}]},
        {"a": float("inf")},
    ],
)
def test_canonical_rejects_floats_anywhere(bad):
    with pytest.raises(ValueError, match="float"):
        canonical_json(bad)


def test_canonical_accepts_max_safe_int():
    assert canonical_json({"a": MAX_SAFE_INT, "b": -MAX_SAFE_INT}) == (
        b'{"a":9007199254740991,"b":-9007199254740991}'
    )


@pytest.mark.parametrize("bad", [MAX_SAFE_INT + 1, -MAX_SAFE_INT - 1, 2**64])
def test_canonical_rejects_unsafe_ints(bad):
    with pytest.raises(ValueError, match="int outside"):
        canonical_json({"a": [{"b": bad}]})


def test_sign_record_rejects_unsafe_ts(keys):
    with pytest.raises(ValueError):
        make(keys, ts=MAX_SAFE_INT + 1)


def test_canonical_rejects_non_string_keys():
    with pytest.raises(ValueError, match="non-string key"):
        canonical_json({"a": {1: "x"}})


def test_canonical_rejects_unserializable_types():
    with pytest.raises(TypeError):
        canonical_json({"a": b"bytes"})


# --- record_id -------------------------------------------------------------


def test_record_id_ignores_id_and_sig():
    unsigned = {"v": 1, "seq": 1}
    assert record_id(unsigned) == record_id({**unsigned, "id": "x", "sig": "y"})


def test_record_id_is_sha256_hex():
    rid = record_id({"v": 1})
    assert len(rid) == 64 and rid == rid.lower()


def test_record_id_changes_with_content():
    assert record_id({"v": 1, "seq": 1}) != record_id({"v": 1, "seq": 2})


# --- sign_record -----------------------------------------------------------


def test_sign_record_envelope(keys):
    r = make(keys)
    assert set(r) == {"v", "author", "seq", "prev", "type", "ts", "payload", "id", "sig"}
    assert r["v"] == FORMAT_VERSION
    assert r["author"] == keys.public
    assert r["seq"] == 1 and r["prev"] is None
    assert r["type"] == "note" and r["ts"] == 1_000 and r["payload"] == {"msg": "hi"}


def test_sign_record_id_matches_record_id(keys):
    r = make(keys)
    assert r["id"] == record_id(r)


def test_sign_record_signature_verifies_over_canonical_bytes(keys):
    r = make(keys)
    unsigned = {k: v for k, v in r.items() if k not in ("id", "sig")}
    # raises nacl.exceptions.BadSignatureError if wrong
    VerifyKey(bytes.fromhex(r["author"])).verify(canonical_json(unsigned), bytes.fromhex(r["sig"]))


def test_sign_record_is_deterministic(keys):
    # ed25519 signatures are deterministic, so the whole record should be too
    assert make(keys) == make(keys)


def test_sign_record_accepts_chained_record(keys):
    first = make(keys)
    second = make(keys, seq=2, prev=first["id"])
    assert second["prev"] == first["id"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"seq": 0},
        {"seq": -1},
        {"seq": True},  # bool is an int subclass; must still be rejected
        {"seq": "1"},
        {"seq": 1, "prev": PREV},  # first record can't have a prev
        {"seq": 2, "prev": None},  # later records must have a prev
        {"seq": 2, "prev": "ab" * 31},  # too short
        {"seq": 2, "prev": "AB" * 32},  # uppercase hex
        {"seq": 2, "prev": "zz" * 32},  # not hex
        {"type": ""},
        {"type": 5},
        {"ts": -1},
        {"ts": 1.0},
        {"ts": False},
        {"payload": ["not", "a", "dict"]},
        {"payload": {"x": 0.5}},  # float caught by canonical_json
    ],
)
def test_sign_record_rejects_bad_input(keys, overrides):
    with pytest.raises(ValueError):
        make(keys, **overrides)


def test_sign_record_size_cap(keys):
    with pytest.raises(ValueError, match="cap"):
        make(keys, payload={"x": "a" * MAX_RECORD_BYTES})


# --- verify_record / tamper detection -------------------------------------


def test_verify_accepts_valid_record(keys):
    assert verify_record(make(keys)) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", {"msg": "bye"}),
        ("seq", 2),
        ("ts", 1_001),
        ("type", "other"),
        ("prev", PREV),
        ("v", FORMAT_VERSION + 1),
    ],
)
def test_verify_rejects_tampered_field(keys, field, value):
    r = make(keys)
    r[field] = value
    assert verify_record(r) is False


def test_verify_rejects_tampered_field_with_recomputed_id(keys):
    # attacker fixes up the id, but can't re-sign without the secret
    r = make(keys)
    r["payload"] = {"msg": "bye"}
    r["id"] = record_id(r)
    assert verify_record(r) is False


def test_verify_rejects_swapped_author(keys):
    r = make(keys)
    r["author"] = Keypair.from_seed(b"\x02" * 32).public
    assert verify_record(r) is False


def test_verify_rejects_bad_sig(keys):
    r = make(keys)
    r["sig"] = "00" * 64
    assert verify_record(r) is False


def test_verify_accepts_chained_record(keys):
    first = make(keys)
    assert verify_record(make(keys, seq=2, prev=first["id"])) is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("sig"),
        lambda r: r.pop("payload"),
        lambda r: r.update(extra=1),  # unsigned extra field
        lambda r: r.update(v=True),  # True == 1 in Python; must not pass as version 1
        lambda r: r.update(author="zz" * 32),
        lambda r: r.update(sig="ab"),
        lambda r: r.update(id=r["id"].upper()),
        lambda r: r.update(seq=0),
        lambda r: r.update(payload={"x": 0.5}),
        lambda r: r.update(payload={"x": b"bytes"}),
        lambda r: r.update(payload="not a dict"),
    ],
)
def test_verify_returns_false_on_malformed_never_raises(keys, mutate):
    r = make(keys)
    mutate(r)
    assert verify_record(r) is False


@pytest.mark.parametrize("bad", [None, [], "record", 42])
def test_verify_non_dict_is_false(bad):
    assert verify_record(bad) is False
