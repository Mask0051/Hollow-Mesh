"""P5 — gossip sync + MemoryTransport. Done when: two nodes converge; random-order test passes."""

from __future__ import annotations

import random
from itertools import pairwise
from typing import Any

import pytest

from hollowmesh.protocol.fake import FakeNode
from hollowmesh.protocol.node import Node
from hollowmesh.protocol.record import Keypair, Record, canonical_json
from hollowmesh.protocol.sync import (
    MAX_BATCH_RECORDS,
    Gossiper,
    SyncNode,
    batch,
    handle_push,
    handle_sync,
    sync_pair,
)
from hollowmesh.protocol.transport.memory import MemoryTransport, Unreachable


def keys(i: int) -> Keypair:
    return Keypair.from_seed(i.to_bytes(32, "big"))


def resident(i: int, cls: type = Node) -> Any:
    """A node that has published its genesis profile."""
    n = cls(keys(i))
    n.publish(type="profile", payload={"kind": "public", "level": i % 4}, now_ms=0)
    return n


def post(n: Any, text: str, t: int = 1) -> Record:
    return n.publish(type="post", payload={"text": text, "level": 0, "sector": "L0-S01"}, now_ms=t)


class Net:
    """A tiny stand-in for the sim's network: nodes plus undirected links."""

    def __init__(self) -> None:
        self.nodes: dict[str, SyncNode] = {}
        self.links: set[frozenset[str]] = set()

    def add(self, *nodes: Any) -> None:
        for n in nodes:
            self.nodes[n.node_id] = n

    def link(self, a: Any, b: Any) -> None:
        self.links.add(frozenset((a.node_id, b.node_id)))

    def cut(self, a: Any, b: Any) -> None:
        self.links.discard(frozenset((a.node_id, b.node_id)))

    def reachable(self, node_id: str) -> list[str]:
        return sorted(p for link in self.links if node_id in link for p in link if p != node_id)

    def node(self, node_id: str) -> SyncNode:
        return self.nodes[node_id]


def gossipers(net: Net, seed: int = 7) -> list[Gossiper]:
    rng = random.Random(seed)  # one RNG for the whole run, like the sim
    return [
        Gossiper(n, MemoryTransport(nid, net), rng)  # type: ignore[arg-type]
        for nid, n in sorted(net.nodes.items())
    ]


def converged(nodes: list[Any]) -> bool:
    first = nodes[0].all_records()
    return all(n.all_records() == first for n in nodes[1:])


# --- batching -----------------------------------------------------------------------------


def _feed(seed: int, n: int) -> list[Record]:
    a = resident(seed)
    for i in range(n - 1):
        post(a, f"{seed}-{i}")
    return a.all_records()


def test_batch_round_robin_keeps_each_author_a_prefix():
    a, b = _feed(1, 5), _feed(2, 2)
    records = sorted(a + b, key=lambda r: (r["author"], r["seq"]))
    out, more = batch(records, limit=4)
    assert more is True
    assert len(out) == 4
    # one from each author per pass: both authors are represented
    assert {r["author"] for r in out} == {a[0]["author"], b[0]["author"]}
    # each author's part is seqs 1..k with no holes, so the receiver never sees a gap
    for author in {r["author"] for r in out}:
        seqs = [r["seq"] for r in out if r["author"] == author]
        assert seqs == list(range(1, len(seqs) + 1))


def test_batch_takes_everything_when_it_fits():
    recs = _feed(1, 3)
    assert batch(recs) == (recs, False)
    assert batch([]) == ([], False)


def test_batch_caps_at_500_even_if_peer_asks_for_more():
    recs = _feed(1, MAX_BATCH_RECORDS + 20)
    out, more = batch(recs, limit=10_000)
    assert len(out) == MAX_BATCH_RECORDS and more


def test_batch_respects_byte_budget():
    recs = _feed(1, 10)
    one = len(canonical_json(recs[0]))
    out, more = batch(recs, max_bytes=one * 3)
    assert 1 <= len(out) <= 3 and more


# --- handlers -----------------------------------------------------------------------------


def test_handle_sync_sends_what_requester_lacks():
    b = resident(2)
    r2 = post(b, "hi")
    resp = handle_sync(b, {"from": "a", "vv": {b.node_id: 1}, "limit": 500})
    assert resp["records"] == [r2]
    assert resp["vv"] == {b.node_id: 2}
    assert resp["from"] == b.node_id and resp["more"] is False


@pytest.mark.parametrize("vv", [None, "junk", {1: 2}, {"x": "y"}, {"x": True}])
def test_handle_sync_tolerates_garbage_vv(vv):
    b = resident(2)
    resp = handle_sync(b, {"from": "a", "vv": vv, "limit": "lots"})  # type: ignore[typeddict-item]
    assert resp["records"] == b.all_records()


def test_handle_push_counts_outcomes_and_blames_sender():
    a = resident(1)
    r2, r3 = post(a, "two"), post(a, "three")
    p1 = a.all_records()[0]
    lie = dict(r2, payload={"text": "LIES", "level": 0, "sector": "L0-S01"})

    b = Node(keys(9))
    res = handle_push(b, {"from": "relay", "records": [p1, r3, p1, lie, "junk"]})  # type: ignore[list-item]
    assert res["accepted"] == 1  # p1
    assert res["pending"] == 1  # r3, waiting for seq 2
    assert {x["reason"] for x in res["rejected"]} == {"tampered", "invalid"}  # dup not listed
    assert b._strikes == {"relay": 1}


def test_handle_push_tolerates_garbage_message():
    b = Node(keys(9))
    assert handle_push(b, {"from": 5, "records": "no"}) == {  # type: ignore[typeddict-item]
        "accepted": 0, "pending": 0, "rejected": [],
    }


# --- two nodes converge (done-when, part 1) ----------------------------------------------


def test_sync_pair_converges_both_ways():
    a, b = resident(1), resident(2)
    post(a, "from a")
    post(b, "from b")
    moved = sync_pair(a, b)
    assert moved == 4  # a's 2 records to b, b's 2 to a
    assert converged([a, b])
    assert a.version_vector() == b.version_vector()
    assert sync_pair(a, b) == 0  # nothing left to move


def test_sync_pair_takes_several_rounds_past_one_batch():
    a, b = resident(1), resident(2)
    for i in range(MAX_BATCH_RECORDS + 50):
        post(a, str(i))
    sync_pair(a, b)
    assert converged([a, b])


def test_fakenode_and_node_sync_with_each_other():
    real, fake = resident(1), resident(2, FakeNode)
    post(real, "real")
    post(fake, "fake")
    sync_pair(real, fake)
    assert real.version_vector() == fake.version_vector()


def test_relayed_through_a_middle_node():
    a, mid, c = resident(1), resident(2), resident(3)
    post(a, "to c")
    sync_pair(a, mid)
    sync_pair(mid, c)
    assert {r["id"] for r in a.all_records()} <= {r["id"] for r in c.all_records()}


# --- MemoryTransport ----------------------------------------------------------------------


def test_transport_refuses_unreachable_peer():
    a, b = resident(1), resident(2)
    net = Net()
    net.add(a, b)
    t = MemoryTransport(a.node_id, net)
    assert t.reachable() == []
    with pytest.raises(Unreachable):
        t.sync(b.node_id, {"from": a.node_id, "vv": {}, "limit": 500})
    net.link(a, b)
    assert t.reachable() == [b.node_id]
    assert t.sync(b.node_id, {"from": a.node_id, "vv": {}, "limit": 500})["records"]


# --- Gossiper -----------------------------------------------------------------------------


def test_pick_peers_prefers_least_recently_synced():
    hub, *others = [resident(i) for i in range(1, 6)]
    net = Net()
    net.add(hub, *others)
    for o in others:
        net.link(hub, o)
    g = Gossiper(hub, MemoryTransport(hub.node_id, net), random.Random(1))
    g.gossip_round()
    first = set(g._last_sync)
    g.gossip_round()
    # 2 rounds x 2 peers = all 4 neighbours: round 2 skips the 2 synced in round 1
    assert len(first) == 2
    assert len(g._last_sync) == 4
    assert {p for p, rnd in g._last_sync.items() if rnd == 2}.isdisjoint(first)
    g.gossip_round()  # round 3: everyone synced once, so the oldest (round 1) go again
    assert {p for p, rnd in g._last_sync.items() if rnd == 3} == first


def test_pick_peers_is_deterministic_for_a_seed():
    def run(seed: int) -> list[list[str]]:
        hub, *others = [resident(i) for i in range(1, 7)]
        net = Net()
        net.add(hub, *others)
        for o in others:
            net.link(hub, o)
        g = Gossiper(hub, MemoryTransport(hub.node_id, net), random.Random(seed))
        out = []
        for _ in range(4):
            out.append(g.pick_peers())
            g.gossip_round()
        return out

    assert run(3) == run(3)


def test_publish_pushes_to_up_to_three_peers():
    a, *peers = [resident(i) for i in range(1, 6)]
    net = Net()
    net.add(a, *peers)
    for p in peers:
        net.link(a, p)
    for p in peers:  # give peers a's genesis so the new post chains straight away
        sync_pair(a, p)
    g = Gossiper(a, MemoryTransport(a.node_id, net), random.Random(0))
    r = g.publish(type="post", payload={"text": "news", "level": 0, "sector": "L0-S01"}, now_ms=5)
    holders = [p for p in peers if p.version_vector().get(a.node_id, 0) >= r["seq"]]
    assert len(holders) == 3


def test_publish_with_no_peers_still_publishes():
    a = resident(1)
    net = Net()
    net.add(a)
    g = Gossiper(a, MemoryTransport(a.node_id, net), random.Random(0))
    g.publish(type="post", payload={"text": "alone", "level": 0, "sector": "L0-S01"}, now_ms=1)
    assert a.version_vector()[a.node_id] == 2


# --- random gossip converges (done-when, part 2) -----------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_random_gossip_converges(seed):
    """Random topology, random publishing, random order of rounds: everyone ends equal."""
    rng = random.Random(seed)
    nodes = [resident(i, rng.choice([Node, Node, FakeNode])) for i in range(1, 9)]
    net = Net()
    net.add(*nodes)
    # a random connected graph: a random spanning chain plus extra random links
    order = nodes[:]
    rng.shuffle(order)
    for x, y in pairwise(order):
        net.link(x, y)
    for _ in range(6):
        net.link(*rng.sample(nodes, 2))

    gs = gossipers(net, seed)
    by_node = {g.node.node_id: g for g in gs}
    for tick in range(10):  # publish while gossiping
        author = rng.choice(nodes)
        by_node[author.node_id].publish(
            type="post", payload={"text": f"t{tick}", "level": 0, "sector": "L0-S01"},
            now_ms=tick,
        )
        for g in rng.sample(gs, len(gs)):  # random order of nodes each tick
            g.gossip_round()

    for _ in range(30):  # then just gossip until quiet
        if converged(nodes):
            break
        for g in rng.sample(gs, len(gs)):
            g.gossip_round()
    assert converged(nodes)
    assert all(n.version_vector() == nodes[0].version_vector() for n in nodes)


def test_partition_then_heal_converges():
    nodes = [resident(i) for i in range(1, 7)]
    left, right = nodes[:3], nodes[3:]
    net = Net()
    net.add(*nodes)
    for half in (left, right):
        for x, y in pairwise(half):
            net.link(x, y)
    gs = gossipers(net)
    by_node = {g.node.node_id: g for g in gs}

    # Split: each side talks only among itself
    by_node[left[0].node_id].publish(
        type="post", payload={"text": "west", "level": 0, "sector": "L0-S01"}, now_ms=1
    )
    by_node[right[0].node_id].publish(
        type="post", payload={"text": "east", "level": 0, "sector": "L0-S01"}, now_ms=1
    )
    for _ in range(10):
        for g in gs:
            g.gossip_round()
    assert converged(left) and converged(right)
    assert not converged(nodes)  # the halves drifted apart
    west_ids = {r["id"] for r in left[0].all_records()}
    assert not west_ids <= {r["id"] for r in right[0].all_records()}

    # Heal: one link across (a stairwell reopens, or someone walks over)
    net.link(left[-1], right[0])
    for _ in range(10):
        for g in gs:
            g.gossip_round()
    assert converged(nodes)


def test_same_seed_same_run():
    def run() -> list[dict[str, int]]:
        nodes = [resident(i) for i in range(1, 6)]
        net = Net()
        net.add(*nodes)
        for x, y in pairwise(nodes):
            net.link(x, y)
        gs = gossipers(net, seed=42)
        gs[0].publish(type="post", payload={"text": "x", "level": 0, "sector": "L0-S01"},
                      now_ms=1)
        history = []
        for _ in range(3):
            for g in gs:
                g.gossip_round()
            history.append(nodes[-1].version_vector())
        return history

    assert run() == run()
