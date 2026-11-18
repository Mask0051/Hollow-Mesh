"""P6 / P7 / H10 — the sim on real nodes: convergence, partitions, the lying relay,
determinism. Uses a smaller Hollow (80 residents) to stay fast."""

from __future__ import annotations

import pytest

from hollowmesh.protocol.node import IngestResult, Node
from hollowmesh.protocol.record import Keypair, check_record
from hollowmesh.protocol.transport.memory import MemoryTransport
from hollowmesh.sim.runner import DEMO_SCRIPT, Sim, VerifyCache, _altered
from hollowmesh.sim.world import build_world

N = 80


@pytest.fixture(scope="module")
def world():
    return build_world()


def run(sim: Sim, ticks: int) -> list[dict]:
    return [sim.tick() for _ in range(ticks)]


def stairwells(sim: Sim) -> list[str]:
    return [e["id"] for e in sim.edges if e["kind"] == "stairwell"]


def levels_holding(sim: Sim, author: str, seq: int) -> list[int]:
    return sorted({sim._level(r) for r in sim.residents if sim.holds(r, author, seq)})


# --- world -------------------------------------------------------------------------------


def test_world_sector_ids_match_payload_format(world):
    assert len(world["sectors"]) == 40
    assert {s["id"] for s in world["sectors"]} >= {"L0-S00", "L3-S09"}
    ids = {s["id"] for s in world["sectors"]}
    assert all(e["a"] in ids and e["b"] in ids for e in world["edges"])


# --- convergence -------------------------------------------------------------------------


def test_every_feed_starts_with_a_profile_and_spreads_everywhere(world):
    sim = Sim(world, seed=1, n_residents=N)
    run(sim, 15)
    vvs = [r["node"].version_vector() for r in sim.residents]
    assert all(vv == vvs[0] for vv in vvs)  # everyone converged
    assert sum(vvs[0].values()) == sim.total_published
    assert sim.snapshot()["syncPct"] == 1.0


def test_injected_report_reaches_every_level(world):
    sim = Sim(world, seed=2, n_residents=N)
    sim.queue_cmd({"cmd": "inject", "sector": "L3-S04"})
    run(sim, 20)
    assert sim.snapshot()["probeLevels"] == [0, 1, 2, 3]


# --- attacks change reachability (H5) ----------------------------------------------------


def test_cut_stairwells_and_network_isolate_a_level_until_heal(world):
    sim = Sim(world, seed=3, n_residents=N)
    run(sim, 5)  # let the profiles spread first
    sim.queue_cmd({"cmd": "cut_network"})
    for sid in stairwells(sim):
        if sid.startswith("ST-2-3"):  # every stairwell between levels 2 and 3
            sim.queue_cmd({"cmd": "cut_stairwell", "id": sid})
    sim.tick()
    deep = next(r for r in sim.residents if sim._level(r) == 3)
    rec = sim._publish(deep, "post", {"text": "help", "level": 3, "sector": "L3-S04"})
    run(sim, 30)
    assert levels_holding(sim, rec["author"], rec["seq"]) == [3]  # nothing got out

    sim.queue_cmd({"cmd": "heal"})
    run(sim, 30)
    assert levels_holding(sim, rec["author"], rec["seq"]) == [0, 1, 2, 3]


def test_network_cut_still_spreads_through_the_mesh(world):
    sim = Sim(world, seed=4, n_residents=N)
    sim.queue_cmd({"cmd": "cut_network"})
    sim.queue_cmd({"cmd": "inject", "sector": "L3-S04"})
    run(sim, 40)
    assert sim.snapshot()["probeLevels"] == [0, 1, 2, 3]


def test_blackout_is_reflected_in_the_snapshot(world):
    sim = Sim(world, seed=5, n_residents=N)
    sim.queue_cmd({"cmd": "blackout", "level": 2})
    snap = sim.tick()
    assert snap["dark"] == [2]
    assert 1 in snap["edges"]  # links touching level 2 show as degraded


def test_junk_commands_are_ignored(world):
    sim = Sim(world, seed=6, n_residents=N)
    for junk in ["x", 5, None, {}, {"cmd": "blackout", "level": "2"},
                 {"cmd": "blackout", "level": 9}, {"cmd": "inject", "sector": "nowhere"},
                 {"cmd": "cut_stairwell", "id": 7}, {"cmd": "nope"}]:
        sim.queue_cmd(junk)
    snap = sim.tick()
    assert snap["dark"] == []
    assert sim.probe is None


# --- the lying relay (P7 / H8) -----------------------------------------------------------


def test_lying_relay_raises_a_real_tamper_alert(world):
    sim = Sim(world, seed=7, n_residents=N)
    sim.queue_cmd({"cmd": "cut_network"})  # force traffic over the edges
    for e in sim.edges:
        sim.queue_cmd({"cmd": "tamper", "id": e["id"]})
    alerts = [a for snap in run(sim, 10) for a in snap["alerts"]]
    assert alerts
    a = alerts[0]
    assert a["kind"] == "tamper" and a["evidence"]["problem"] == "bad_id"
    assert a["evidence"]["claimed_id"] != a["evidence"]["computed_id"]
    assert a["peer"] in sim.by_node  # names the relay that sent it
    # One alert per tampered record, not one per node that caught it.
    ids = [a["evidence"]["claimed_id"] for a in alerts]
    assert len(ids) == len(set(ids))


def test_tampering_loses_nothing_once_healed(world):
    sim = Sim(world, seed=8, n_residents=N)
    sim.queue_cmd({"cmd": "cut_network"})
    for sid in stairwells(sim):
        sim.queue_cmd({"cmd": "tamper", "id": sid})
    run(sim, 20)
    sim.queue_cmd({"cmd": "heal"})
    run(sim, 30)
    vvs = [r["node"].version_vector() for r in sim.residents]
    assert all(vv == vvs[0] for vv in vvs)
    assert sum(vvs[0].values()) == sim.total_published


def test_relay_hook_alters_records_in_transit():
    a, b = Node(Keypair.from_seed(b"a" * 32)), Node(Keypair.from_seed(b"b" * 32))
    rec = a.publish(type="profile", payload={"kind": "public", "level": 0}, now_ms=0)

    class Net:
        def reachable(self, node_id):
            return [b.node_id if node_id == a.node_id else a.node_id]

        def node(self, node_id):
            return {a.node_id: a, b.node_id: b}[node_id]

    seen = []
    t = MemoryTransport(a.node_id, Net(),
                        relay=lambda s, r, recs: seen.append((s, r)) or [_altered(x) for x in recs])
    result = t.push(b.node_id, {"from": a.node_id, "records": [rec]})
    assert seen == [(a.node_id, b.node_id)]
    assert result["rejected"] == [{"id": rec["id"], "reason": "tampered"}]
    assert b.ingest(rec) is IngestResult.ACCEPTED  # the honest copy still gets in


def test_verify_cache_agrees_with_check_record():
    node = Node(Keypair.from_seed(b"c" * 32))
    rec = node.publish(type="profile", payload={"kind": "public", "level": 1}, now_ms=0)
    cache = VerifyCache()
    for r in [rec, rec, _altered(rec), dict(rec, sig="00" * 64), {"junk": 1}]:
        assert cache(r) == check_record(r)
        assert cache(r) == check_record(r)  # cached answer too


# --- determinism (H10) -------------------------------------------------------------------


def test_same_seed_same_run(world):
    script = {t: cmds for t, cmds in DEMO_SCRIPT.items() if t <= 60}
    a = run(Sim(world, seed=42, n_residents=N, script=script), 70)
    b = run(Sim(world, seed=42, n_residents=N, script=script), 70)
    assert a == b


def test_different_seed_different_run(world):
    a = run(Sim(world, seed=1, n_residents=N), 5)
    b = run(Sim(world, seed=2, n_residents=N), 5)
    assert a != b
