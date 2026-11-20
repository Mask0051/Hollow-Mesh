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
    assert len(world["sectors"]) == 33
    assert {s["id"] for s in world["sectors"]} >= {"L0-S00", "L1-S09", "L3-S05"}
    ids = {s["id"] for s in world["sectors"]}
    assert all(e["a"] in ids and e["b"] in ids for e in world["edges"])


# --- convergence -------------------------------------------------------------------------


@pytest.fixture
def quiet(monkeypatch):
    """No background traffic (chatter, check-ins): a convergence check sees a still mesh."""
    monkeypatch.setattr("hollowmesh.sim.runner.POST_P", 0)
    monkeypatch.setattr("hollowmesh.sim.runner.CHECKIN_P", 0)


def test_every_feed_starts_with_a_profile_and_spreads_everywhere(world, quiet):
    sim = Sim(world, seed=1, n_residents=N, sickness=False)
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


def test_tampering_loses_nothing_once_healed(world, quiet):
    # No background traffic: a record sent on the last tick would still be in flight.
    sim = Sim(world, seed=8, n_residents=N, sickness=False)
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


# --- phones and beacon colours (demo) ----------------------------------------------------


def test_everyone_starts_the_default_colour(world):
    from hollowmesh.sim.runner import DEFAULT_COLOR

    sim = Sim(world, seed=9, n_residents=N)
    assert set(sim.tick()["colors"]) == {DEFAULT_COLOR}


def test_beacon_colour_spreads_and_newest_wins(world):
    sim = Sim(world, seed=10, n_residents=N)
    run(sim, 3)
    sim.queue_cmd({"cmd": "beacon", "resident": 5, "color": "#FF3B30", "label": "red"})
    snap = sim.tick()
    assert snap["colors"][5] == "#ff3b30"  # the sender sees it at once (lower-cased)
    snaps = run(sim, 15)
    assert set(snaps[-1]["colors"]) == {"#ff3b30"}  # with the network up it reaches everyone
    sim.queue_cmd({"cmd": "beacon", "resident": 9, "color": "#3b82f6", "label": "blue"})
    assert set(run(sim, 15)[-1]["colors"]) == {"#3b82f6"}


def test_beacon_only_colours_who_it_reached(world):
    sim = Sim(world, seed=11, n_residents=N)
    run(sim, 5)
    sim.queue_cmd({"cmd": "cut_network"})
    for sid in stairwells(sim):
        sim.queue_cmd({"cmd": "cut_stairwell", "id": sid})
    sim.tick()
    sender = next(r for r in sim.residents if sim._level(r) == 3)
    sim.queue_cmd({"cmd": "beacon", "resident": sender["id"], "color": "#ff3b30"})
    snap = run(sim, 60)[-1]
    for r in sim.residents:  # cut off from level 3: still the default colour
        assert (snap["colors"][r["id"]] == "#ff3b30") == (sim._level(r) == 3)


def test_phone_shows_what_that_node_holds(world):
    sim = Sim(world, seed=12, n_residents=N)
    run(sim, 3)
    sim.queue_cmd({"cmd": "post", "resident": 0, "text": "hello from r0"})
    sim.tick()
    mine = sim.phone(0)
    assert mine["name"] == "r0" and mine["feed"][0]["mine"]
    assert mine["feed"][0]["text"] == "hello from r0"
    assert mine["feed"][0]["sector"] == sim.sectors[sim.residents[0]["sec"]]["id"]
    run(sim, 15)
    theirs = sim.phone(1)
    assert any(f["from"] == "r0" and f["text"] == "hello from r0" for f in theirs["feed"])


def test_bad_phone_commands_are_ignored(world):
    sim = Sim(world, seed=13, n_residents=N)
    before = sim.total_published
    for junk in [{"cmd": "post", "resident": 0, "text": ""},
                 {"cmd": "post", "resident": 0, "text": "x" * 281},
                 {"cmd": "post", "resident": N, "text": "hi"},
                 {"cmd": "post", "resident": True, "text": "hi"},
                 {"cmd": "beacon", "resident": 0, "color": "red"},
                 {"cmd": "beacon", "resident": 0, "color": "#ggg000"},
                 {"cmd": "beacon", "resident": 0, "color": "#ff0000", "label": "x" * 41}]:
        sim.queue_cmd(junk)
    sim.tick()
    assert sim.total_published == before


# --- clicking stairwells -----------------------------------------------------------------


def test_toggle_stairwell_cuts_and_reopens(world):
    sim = Sim(world, seed=14, n_residents=N)
    sid = "ST-1-2-b"
    edge = sim.edges[sim.eidx[sid]]
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": sid})
    snap = sim.tick()
    assert not edge["up"] and snap["edges"][sim.eidx[sid]] == 2
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": sid})
    sim.tick()
    assert edge["up"]


def test_toggle_stairwell_ignores_tunnels_and_unknown_ids(world):
    sim = Sim(world, seed=15, n_residents=N)
    tunnel = next(e for e in sim.edges if e["kind"] == "tunnel")
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": tunnel["id"]})
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": "ST-9-9-z"})
    sim.tick()
    assert all(e["up"] for e in sim.edges)


def test_unhashable_ids_from_the_browser_dont_crash(world):
    sim = Sim(world, seed=16, n_residents=N)
    for cmd in ("toggle_stairwell", "cut_stairwell", "tamper", "inject"):
        sim.queue_cmd({"cmd": cmd, "id": [1], "edge": {"a": 1}, "sector": ["x"]})
    sim.tick()  # must not raise


# --- Wi-Fi / Rat King switches -----------------------------------------------------------


def spread_with(world, seed: int, *, wifi: bool, mesh: bool, cut_stairs: bool = False) -> Sim:
    """Settle, set the switches, send a red beacon from r0, run 25 ticks."""
    sim = Sim(world, seed=seed, n_residents=N)
    run(sim, 5)
    sim.queue_cmd({"cmd": "network", "on": wifi})
    sim.queue_cmd({"cmd": "mesh", "on": mesh})
    if cut_stairs:
        for sid in stairwells(sim):
            sim.queue_cmd({"cmd": "cut_stairwell", "id": sid})
    sim.queue_cmd({"cmd": "beacon", "resident": 0, "color": "#ff3b30"})
    run(sim, 25)
    return sim


def red(sim: Sim) -> int:
    return sim.snapshot()["colors"].count("#ff3b30")


def test_switches_show_in_the_snapshot(world):
    sim = Sim(world, seed=17, n_residents=N)
    sim.queue_cmd({"cmd": "network", "on": False})
    sim.queue_cmd({"cmd": "mesh", "on": False})
    snap = sim.tick()
    assert (snap["net_up"], snap["mesh_up"]) == (False, False)
    sim.queue_cmd({"cmd": "heal"})
    snap = sim.tick()
    assert (snap["net_up"], snap["mesh_up"]) == (True, True)


def test_nothing_on_nothing_spreads(world):
    sim = spread_with(world, 18, wifi=False, mesh=False)
    assert red(sim) == 1  # only the sender
    assert all(not sim.reachable(r["node"].node_id) for r in sim.residents)


def test_wifi_alone_reaches_everyone_even_with_stairs_cut(world):
    assert red(spread_with(world, 19, wifi=True, mesh=False, cut_stairs=True)) == N


def test_mesh_alone_reaches_everyone_without_wifi(world):
    assert red(spread_with(world, 20, wifi=False, mesh=True)) == N


def test_mesh_alone_stops_at_cut_stairwells(world):
    sim = spread_with(world, 21, wifi=False, mesh=True, cut_stairs=True)
    # Everyone's stuck in the part they were in: their level, or one Farms wing.
    home = tunnel_component(sim, sim.residents[0]["sec"])
    for r in sim.residents:
        assert (sim.snapshot()["colors"][r["id"]] == "#ff3b30") == (r["sec"] in home)


def tunnel_component(sim: Sim, sec: int) -> set[int]:
    seen, todo = {sec}, [sec]
    while todo:
        for ei, b in sim.adj[todo.pop()]:
            if sim.edges[ei]["kind"] == "tunnel" and b not in seen:
                seen.add(b)
                todo.append(b)
    return seen


def test_bad_switch_values_are_ignored(world):
    sim = Sim(world, seed=22, n_residents=N)
    for v in ["off", 0, None, [False]]:
        sim.queue_cmd({"cmd": "mesh", "on": v})
        sim.queue_cmd({"cmd": "network", "on": v})
    snap = sim.tick()
    assert snap["net_up"] and snap["mesh_up"]


# --- the map's shape is part of the demo --------------------------------------------------


def stairs_between(world, lv: int) -> list[str]:
    return [e["id"] for e in world["edges"]
            if e["kind"] == "stairwell" and e["id"].startswith(f"ST-{lv}-{lv + 1}-")]


def test_fewer_stairwells_the_deeper_you_go(world):
    assert [len(stairs_between(world, lv)) for lv in range(3)] == [3, 2, 1]


def test_farms_has_two_wings_joined_only_from_above(world):
    sim = Sim(world, seed=27, n_residents=10)
    farms = [i for i, s in enumerate(sim.sectors) if s["level"] == 2]
    west = tunnel_component(sim, farms[0])
    assert 0 < len(west) < len(farms)  # the collapsed tunnel splits it
    lv1_to_lv2 = [e for e in world["edges"] if e["id"] in stairs_between(world, 1)]
    assert {sim.idx[e["b"]] in west for e in lv1_to_lv2} == {True, False}  # one per wing


def test_stairwell_columns_sit_inside_both_rooms(world):
    by = {s["id"]: s for s in world["sectors"]}
    for e in world["edges"]:
        if e["kind"] == "stairwell":
            for s in (by[e["a"]], by[e["b"]]):
                assert abs(e["x"] - s["x"]) < s["w"] / 2


def test_cutting_the_one_shaft_isolates_the_deep(world):
    sim = Sim(world, seed=28, n_residents=N)
    run(sim, 5)
    sim.queue_cmd({"cmd": "network", "on": False})
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": "ST-2-3-a"})
    sim.queue_cmd({"cmd": "inject", "sector": "L3-S04"})
    run(sim, 40)
    assert sim.snapshot()["probeLevels"] == [3]
    sim.queue_cmd({"cmd": "toggle_stairwell", "id": "ST-2-3-a"})  # reopen: it climbs out
    run(sim, 60)
    assert sim.snapshot()["probeLevels"] == [0, 1, 2, 3]


# --- dashboard toggles: power per level, lying relay on/off --------------------------------


def test_power_switch_cuts_and_restores_a_level(world):
    sim = Sim(world, seed=23, n_residents=N)
    sim.queue_cmd({"cmd": "power", "level": 1, "on": False})
    sim.queue_cmd({"cmd": "power", "level": 3, "on": False})
    assert sim.tick()["dark"] == [1, 3]
    sim.queue_cmd({"cmd": "power", "level": 1, "on": True})
    assert sim.tick()["dark"] == [3]
    for junk in [{"cmd": "power", "level": 0, "on": "no"}, {"cmd": "power", "level": True,
                 "on": False}, {"cmd": "power", "level": 7, "on": False}]:
        sim.queue_cmd(junk)
    assert sim.tick()["dark"] == [3]


def test_relay_switch_deploys_and_removes_the_liar(world):
    sim = Sim(world, seed=24, n_residents=N)
    sim.queue_cmd({"cmd": "relay", "on": True})
    assert len(sim.tick()["lying"]) == 1
    sim.queue_cmd({"cmd": "relay", "on": "off"})  # junk: ignored
    assert len(sim.tick()["lying"]) == 1
    sim.queue_cmd({"cmd": "relay", "on": False})
    assert sim.tick()["lying"] == []


def test_dark_level_gets_no_wifi(world):
    # Wi-Fi only, stairs cut (nobody can carry it between levels): a dark level stays cut off.
    sim = Sim(world, seed=25, n_residents=N)
    run(sim, 5)
    for sid in stairwells(sim):
        sim.queue_cmd({"cmd": "cut_stairwell", "id": sid})
    sim.queue_cmd({"cmd": "mesh", "on": False})
    run(sim, 1)
    dark = (sim._level(sim.residents[0]) + 1) % sim.levels
    sim.queue_cmd({"cmd": "power", "level": dark, "on": False})
    sim.queue_cmd({"cmd": "beacon", "resident": 0, "color": "#ff3b30"})
    run(sim, 25)
    colors = sim.snapshot()["colors"]
    for r in sim.residents:
        assert (colors[r["id"]] == "#ff3b30") == (sim._level(r) != dark)
    sim.queue_cmd({"cmd": "power", "level": dark, "on": True})  # power back: Wi-Fi back
    run(sim, 25)
    assert red(sim) == N


def test_mesh_still_works_on_a_dark_level(world):
    sim = Sim(world, seed=26, n_residents=N)
    run(sim, 5)
    sim.queue_cmd({"cmd": "network", "on": False})
    for lv in range(sim.levels):
        sim.queue_cmd({"cmd": "power", "level": lv, "on": False})
    sim.queue_cmd({"cmd": "beacon", "resident": 0, "color": "#ff3b30"})
    run(sim, 60)
    assert red(sim) > N // 2  # slower (only ~30% of phones up each tick), but it spreads


# --- phone tabs: one per record type --------------------------------------------------------


def test_phone_sends_sos_and_health_reports_into_their_tabs(world):
    sim = Sim(world, seed=29, n_residents=N)
    run(sim, 300)  # past day 1, so "2 days ago" has somewhere to go
    sim.queue_cmd({"cmd": "help", "resident": 0, "kind": "medical", "urgency": 2,
                   "people": 3, "text": "broken leg"})
    sim.queue_cmd({"cmd": "health", "resident": 0, "sym": ["cough", "fever"], "severity": 2,
                   "sick": 4, "ago": 1})
    sim.queue_cmd({"cmd": "beacon", "resident": 0, "color": "#22c55e"})
    run(sim, 1)
    p = sim.phone(0)
    sos, health = p["tabs"]["help_request"][0], p["tabs"]["health_report"][0]
    assert sos["mine"] and sos["p"]["kind"] == "medical" and sos["p"]["people"] == 3
    assert health["p"]["sym"] == ["cough", "fever"] and health["p"]["onset_day"] == p["day"] - 1
    here = sim.sectors[sim.residents[0]["sec"]]
    assert sos["p"]["sector"] == health["p"]["sector"] == here["id"]  # stamped at send time
    assert any(f["type"] == "beacon" and f["color"] == "#22c55e" for f in p["feed"])  # in All
    assert all(e["type"] == t for t, es in p["tabs"].items() for e in es)
    assert p["counts"]["help_request"] == len(p["tabs"]["help_request"])
    run(sim, 30)
    assert any(e["from"] == "r0" for e in sim.phone(1)["tabs"]["health_report"])  # it spreads


def test_bad_sos_and_health_reports_are_dropped(world):
    sim = Sim(world, seed=30, n_residents=N)
    good_help = {"cmd": "help", "resident": 0, "kind": "air", "urgency": 1, "people": 0,
                 "text": ""}
    good_health = {"cmd": "health", "resident": 0, "sym": ["chest"], "severity": 1, "sick": 1}
    for junk in [{**good_help, "kind": "lost"}, {**good_help, "urgency": 9},
                 {**good_help, "people": "2"}, {**good_help, "text": "x" * 281},
                 {**good_health, "sym": ["rash"]}, {**good_health, "sym": []},
                 {**good_health, "sick": 0}, {**good_health, "ago": -1},
                 {**good_health, "ago": True}, {**good_health, "resident": 10_000},
                 {**good_health, "sym": [["cough"]]}]:
        sim.queue_cmd(junk)
    run(sim, 1)
    assert sim.phone(0)["counts"]["help_request"] == sim.phone(0)["counts"]["health_report"] == 0
    sim.queue_cmd(good_help)
    sim.queue_cmd(good_health)  # "ago" defaults to today; day 0 is fine
    run(sim, 1)
    assert sim.phone(0)["counts"]["health_report"] == 1
    assert sim.phone(0)["tabs"]["health_report"][0]["p"]["onset_day"] == 0


# --- Countertally + sickness (P8 / H9) ---------------------------------------------------


def test_sick_residents_file_real_health_reports(world):
    sim = Sim(world, seed=3, n_residents=N)
    run(sim, 150)
    cases = sim.sickness.cases
    assert cases, "nobody got sick"
    filed = [c for c in cases if c.report_at is not None and c.report_at <= sim.t]
    reports = [x for x in sim.residents[0]["node"].all_records() if x["type"] == "health_report"]
    assert len(reports) <= len(filed) and filed
    authors = {r["node"].node_id: r["id"] for r in sim.residents}
    for x in reports:  # each one came from the sick resident's own phone
        c = sim.sickness.had[authors[x["author"]]]
        assert x["payload"]["sector"] == c.sector and x["payload"]["sick"] == c.sick


def test_deeper_levels_get_sicker(world):
    truth = [0, 0, 0, 0]
    for seed in range(4):
        sim = Sim(world, seed=seed, n_residents=N)
        run(sim, 200)
        truth = [a + b for a, b in zip(truth, sim.stats()["truth"], strict=True)]
    assert truth[3] > truth[0]


def test_countertally_beats_the_official_number_and_never_exceeds_truth(world):
    sim = Sim(world, seed=1, n_residents=N)
    run(sim, 250)
    st = sim.stats()
    assert sum(st["countertally"]) > sum(st["official"])
    for ours, real in zip(st["countertally"], st["truth"], strict=True):
        assert ours <= real  # we only count what was really reported


def test_a_cut_off_level_goes_stale_not_zero(world):
    sim = Sim(world, seed=2, n_residents=N)
    sim.queue_cmd({"cmd": "cut_network"})
    sim.queue_cmd({"cmd": "cut_stairwell", "id": "ST-2-3-a"})
    run(sim, 1)
    obs = next(r["id"] for r in sim.residents if sim._level(r) != 3)
    sim.queue_cmd({"cmd": "observe", "resident": obs})
    run(sim, 80)
    st = sim.stats()
    assert sim._level(sim.residents[obs]) != 3  # nobody walks through a cut shaft
    assert st["staleness"][3] is None or st["staleness"][3] >= 70  # unknown or old, never fresh
    assert sum(st["truth"]) >= 0 and st["observer"]["id"] == obs


def test_observe_command_validates(world):
    sim = Sim(world, seed=1, n_residents=N)
    before = sim.observer
    for bad in (-1, N, True, "3", None, 2.0):
        sim.queue_cmd({"cmd": "observe", "resident": bad})
    run(sim, 1)
    assert sim.observer == before
    sim.queue_cmd({"cmd": "observe", "resident": 7})
    run(sim, 1)
    assert sim.observer == 7 and sim.stats()["observer"]["name"] == "r7"


def test_sickness_can_be_switched_off(world):
    sim = Sim(world, seed=1, n_residents=N, sickness=False)
    snap = run(sim, 100)[-1]
    assert snap["sick"] == [] and snap["stats"]["truth"] == [0, 0, 0, 0]


def test_sos_markers_show_where_it_came_from_and_how_far_it_got(world):
    sim = Sim(world, seed=2, n_residents=N, sickness=False)
    sim.queue_cmd({"cmd": "cut_network"})
    sim.queue_cmd({"cmd": "cut_stairwell", "id": "ST-2-3-a"})
    sim.queue_cmd({"cmd": "inject", "sector": "L3-S04"})
    snap = run(sim, 10)[-1]
    (m,) = snap["sos"]
    assert sim.sectors[m["sector"]]["level"] == 3 and m["kind"] == "air" and m["people"] == 12
    assert m["levels"] == [3]  # the shaft is cut: it can't leave the Deep
    sim.queue_cmd({"cmd": "heal"})
    assert run(sim, 40)[-1]["sos"][0]["levels"] == [0, 1, 2, 3]
    run(sim, 150)
    assert sim.snapshot()["sos"] == []  # older than a sim day: off the map
