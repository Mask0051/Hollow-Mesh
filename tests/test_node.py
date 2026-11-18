"""P3 — Node ingest: append, out-of-order, gap, tamper, duplicate (Doc 4 done-when),
plus invalid input, forks, order independence and parity with FakeNode."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hollowmesh.protocol.fake import FakeNode
from hollowmesh.protocol.node import Alert, IngestResult, Node
from hollowmesh.protocol.record import Keypair, Record, record_id, sign_record

R = IngestResult
PEER = "peer-1"
EVIL = "lying-relay"


def keys(i: int) -> Keypair:
    return Keypair.from_seed(bytes([i]) * 32)


def post(text: str, level: int = 2) -> dict[str, Any]:
    return {"text": text, "level": level, "sector": f"L{level}-S07"}


def make_feed(seed: int, n_posts: int) -> list[Record]:
    """An author's real feed: profile, then n_posts posts."""
    author = Node(keys(seed))
    feed = [author.publish(type="profile", payload={"kind": "public", "level": 2}, now_ms=0)]
    feed += [
        author.publish(type="post", payload=post(f"msg {i}"), now_ms=i + 1)
        for i in range(n_posts)
    ]
    return feed


# Built once: signing is deterministic, so these are identical every run.
FEED_A = make_feed(1, 3)  # seq 1..4
FEED_C = make_feed(3, 5)  # seq 1..6
A = FEED_A[0]["author"]
C = FEED_C[0]["author"]


@pytest.fixture
def events() -> list[Alert]:
    return []


@pytest.fixture
def node(events: list[Alert]) -> Node:
    """The receiving node under test; its alerts land in `events`."""
    return Node(keys(9), on_event=events.append)


def tampered(record: Record, **changes: Any) -> dict[str, Any]:
    """A copy with some fields changed, as a lying relay would send it."""
    out = dict(record)
    out.update(changes)
    return out


# --- publish ----------------------------------------------------------------------------


def test_publish_builds_a_chain():
    n = Node(keys(1))
    r1 = n.publish(type="profile", payload={"kind": "public", "level": 2}, now_ms=100)
    r2 = n.publish(type="post", payload=post("hi"), now_ms=50)  # clock went backwards
    assert (r1["seq"], r1["prev"]) == (1, None)
    assert (r2["seq"], r2["prev"]) == (2, r1["id"])
    assert r2["ts"] == 100  # ts never goes backwards within a feed
    assert n.version_vector() == {n.node_id: 2}
    assert n.all_records() == [r1, r2]


def test_publish_private_identity_is_a_separate_feed():
    n = Node(keys(1), private_keys=keys(2))
    pub = n.publish(type="profile", payload={"kind": "public", "level": 0}, now_ms=0)
    priv = n.publish(
        type="profile", payload={"kind": "private", "level": 0}, now_ms=0, identity="private"
    )
    assert pub["author"] != priv["author"]
    assert priv["seq"] == 1  # its own chain, not seq 2 of the public one


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "post", "payload": post("first")},  # genesis must be a profile
        {"type": "profile", "payload": {"kind": "public", "level": 9}},  # bad schema
        {"type": "nope", "payload": {}},  # unknown type
        {"type": "profile", "payload": {"kind": "public", "level": 0}, "identity": "private"},
    ],
)
def test_publish_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        Node(keys(1)).publish(now_ms=0, **kwargs)


# --- 1. append --------------------------------------------------------------------------


def test_append_in_order(node, events):
    assert [node.ingest(r, PEER) for r in FEED_A] == [R.ACCEPTED] * 4
    assert node.version_vector() == {A: 4}
    assert node.all_records() == FEED_A
    assert events == []


# --- 2. out-of-order --------------------------------------------------------------------


def test_out_of_order_chains_once_complete(node, events):
    results = [node.ingest(r, PEER) for r in reversed(FEED_A)]
    assert results == [R.GAP, R.GAP, R.GAP, R.ACCEPTED]
    assert node.all_records() == FEED_A
    assert node.version_vector() == {A: 4}
    assert node._pending == {}
    assert node._status[A] == "ok"
    assert [e["kind"] for e in events] == ["gap"]  # one alert, not one per record


# --- 3. gap -----------------------------------------------------------------------------


def test_gap_holds_records_and_alerts_once(node, events):
    p1, _, p3, p4 = FEED_A
    assert node.ingest(p1, PEER) is R.ACCEPTED
    assert node.ingest(p3, PEER) is R.GAP
    assert node.ingest(p4, PEER) is R.GAP

    assert node.version_vector() == {A: 1}  # pending never counts
    assert node._status[A] == "gapped"
    assert len(events) == 1
    alert = events[0]
    assert alert["kind"] == "gap" and alert["author"] == A and alert["peer"] == PEER
    assert alert["seq"] == 2  # the first missing seq
    assert alert["evidence"] == {"have": 1, "got": 3}


def test_gapped_records_are_not_forwarded(node):
    p1, _, p3, _ = FEED_A
    node.ingest(p1)
    node.ingest(p3)
    assert node.records_since({}) == [p1]
    assert node.all_records() == [p1]


def test_filling_the_gap_drains_pending(node):
    p1, p2, p3, p4 = FEED_A
    for r in (p1, p3, p4):
        node.ingest(r)
    assert node.ingest(p2) is R.ACCEPTED
    assert node.version_vector() == {A: 4}
    assert node._status[A] == "ok"
    assert node._pending == {}


# --- 4. tamper --------------------------------------------------------------------------


def test_tamper_altered_payload_with_old_id(node, events):
    node.ingest(FEED_A[0])
    bad = tampered(FEED_A[1], payload=post("LIES"))
    assert node.ingest(bad, EVIL) is R.TAMPERED
    ev = events[-1]["evidence"]
    assert events[-1]["kind"] == "tamper" and events[-1]["peer"] == EVIL
    assert ev["problem"] == "bad_id"
    assert ev["claimed_id"] == FEED_A[1]["id"]
    assert ev["computed_id"] == record_id(bad) != ev["claimed_id"]


def test_tamper_altered_payload_with_recomputed_id(node, events):
    bad = tampered(FEED_A[1], payload=post("LIES"))
    bad["id"] = record_id(bad)  # attacker fixes the id but can't re-sign
    assert node.ingest(bad, EVIL) is R.TAMPERED
    assert events[-1]["evidence"]["problem"] == "bad_sig"


def test_tampered_copy_of_held_record_is_not_a_duplicate(node):
    # Verification runs before dedupe, so keeping the real id doesn't sneak it past.
    node.ingest(FEED_A[0])
    node.ingest(FEED_A[1])
    assert node.ingest(tampered(FEED_A[1], payload=post("LIES")), EVIL) is R.TAMPERED


def test_tamper_into_invalid_payload_is_still_tamper(node):
    # The signature is checked before the schema, so this isn't let off as INVALID.
    bad = tampered(FEED_A[1], payload=post("x", level=9))
    assert node.ingest(bad, EVIL) is R.TAMPERED


def test_tamper_stores_nothing_and_strikes_the_peer(node, events):
    node.ingest(FEED_A[0])
    for _ in range(3):
        node.ingest(tampered(FEED_A[1], payload=post("LIES")), EVIL)
    assert node.version_vector() == {A: 1}
    assert node._pending == {}
    assert node._strikes == {EVIL: 3}
    assert len(node._evidence) == 3 == len(events)


def test_tamper_without_peer_gives_no_strike(node):
    assert node.ingest(tampered(FEED_A[1], payload=post("LIES"))) is R.TAMPERED
    assert node._strikes == {}


# --- 5. duplicate -----------------------------------------------------------------------


def test_duplicate_of_held_record(node):
    node.ingest(FEED_A[0])
    assert node.ingest(FEED_A[0], PEER) is R.DUPLICATE


def test_duplicate_of_pending_record(node):
    node.ingest(FEED_A[0])
    node.ingest(FEED_A[2])  # pending
    assert node.ingest(FEED_A[2], PEER) is R.DUPLICATE
    assert sum(len(c) for c in node._pending[A].values()) == 1


# --- 6. invalid -------------------------------------------------------------------------


def _signed(seq: int, type: str, payload: dict[str, Any], prev: str | None = None) -> Record:
    return sign_record(keys(2), seq=seq, prev=prev, type=type, payload=payload, ts=0)


@pytest.mark.parametrize(
    "bad",
    [
        # Properly signed by the author, but the content breaks the rules:
        _signed(1, "profile", {"kind": "public", "level": 9}),  # schema
        _signed(1, "nope", {}),  # unknown type
        _signed(1, "post", post("not a profile")),  # genesis must be a profile
        # Not records at all:
        {},
        {"x": 1},
        None,
        [],
        "record",
        tampered(FEED_A[0], v=2),
        {k: v for k, v in FEED_A[0].items() if k != "sig"},
    ],
)
def test_invalid_is_rejected_without_strike_or_alert(node, events, bad):
    assert node.ingest(bad, PEER) is R.INVALID
    assert node._strikes == {}
    assert events == []
    assert node.all_records() == []


# --- forks ------------------------------------------------------------------------------

FORK_2 = sign_record(
    keys(1), seq=2, prev=FEED_A[0]["id"], type="post", payload=post("other"), ts=1
)
FORK_3 = sign_record(keys(1), seq=3, prev=FORK_2["id"], type="post", payload=post("z"), ts=2)


def test_fork_at_held_seq(node, events):
    for r in FEED_A:
        node.ingest(r)
    assert node.ingest(FORK_2, PEER) is R.FORK
    alert = events[-1]
    assert alert["kind"] == "fork" and alert["seq"] == 2
    assert alert["evidence"]["ours"] == FEED_A[1]
    assert alert["evidence"]["theirs"] == FORK_2
    assert node.all_records() == FEED_A  # the chain we hold is unchanged


def test_suspected_fork_is_reported_when_chain_passes_it(node, events):
    p1, p2, p3, _ = FEED_A
    node.ingest(p1)
    node.ingest(p2)
    assert node.ingest(FORK_3) is R.GAP  # right seq, wrong prev: waits
    assert events == []
    assert node.ingest(p3) is R.ACCEPTED
    assert events[-1]["kind"] == "fork" and events[-1]["seq"] == 3
    assert node._pending == {}
    assert node._status[A] == "ok"


# --- records_since / version_vector -----------------------------------------------------


def test_records_since_sends_only_what_peer_lacks(node):
    for r in FEED_A + FEED_C:
        node.ingest(r)
    assert node.records_since({A: 4, C: 6}) == []
    assert node.records_since({A: 2, C: 6}) == FEED_A[2:]
    assert node.records_since({A: 4}) == FEED_C  # unknown author -> everything
    assert node.records_since({A: 99, C: 99}) == []
    assert node.records_since({A: -5, C: 6}) == FEED_A  # bogus count treated as 0


def test_version_vector_is_a_copy(node):
    node.ingest(FEED_A[0])
    node.version_vector()[A] = 99
    assert node.version_vector() == {A: 1}


def test_two_nodes_converge_by_syncing():
    a, b = Node(keys(10)), Node(keys(11))
    for r in FEED_A:
        a.ingest(r)
    for r in FEED_C:
        b.ingest(r)
    for r in a.records_since(b.version_vector()):
        b.ingest(r)
    for r in b.records_since(a.version_vector()):
        a.ingest(r)
    assert a.all_records() == b.all_records()
    assert a.version_vector() == b.version_vector() == {A: 4, C: 6}


# --- 7. order independence (the convergence property) ----------------------------------


@settings(max_examples=200, deadline=None)
@given(st.permutations(FEED_A + FEED_C))
def test_any_arrival_order_gives_the_same_state(order):
    n = Node(keys(9))
    for r in order:
        n.ingest(r)
    assert n.all_records() == sorted(FEED_A + FEED_C, key=lambda r: (r["author"], r["seq"]))
    assert n._pending == {}


# --- 8. parity with FakeNode (so the P6 swap changes nothing for the sim) ---------------


def test_same_state_as_fakenode():
    fake, real = FakeNode(keys(9)), Node(keys(9))
    order = [FEED_A[0], FEED_A[2], FEED_C[0], FEED_C[2], FEED_C[1], FEED_A[3]]
    for r in order:
        assert fake.ingest(r) is real.ingest(r)
    assert fake.version_vector() == real.version_vector()
    assert fake.records_since({}) == real.records_since({})
