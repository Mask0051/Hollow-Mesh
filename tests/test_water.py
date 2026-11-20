"""Water watch: water_report schema, the unsafe rule, and bad water in the sim."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hollowmesh.protocol.types import WaterReport
from hollowmesh.services.water import water_status
from hollowmesh.sim.runner import Sim
from hollowmesh.sim.world import build_world

TICK, HOUR = 600_000, 3_600_000
NOW = 48 * HOUR
RECS = build_world()["recyclers"]
WR = {"recycler": "R3", "level": 3, "sector": "L3-S02", "issue": "smell", "severity": 2}


def wr(author: str, seq: int, ts: int, **kw) -> dict:
    return {"type": "water_report", "author": author, "seq": seq, "ts": ts, "payload": {**WR, **kw}}


def status(records: list[dict]) -> dict:
    return {s["id"]: s for s in water_status(records, NOW, RECS, TICK)}


def test_schema():
    WaterReport.model_validate(WR)
    for bad in ({**WR, "recycler": "R2"}, {**WR, "sector": "L2-S02"}, {**WR, "issue": "fizzy"},
                {**WR, "severity": 0}, {**WR, "recycler": "R9"}, {**WR, "extra": 1}):
        with pytest.raises(ValidationError):
            WaterReport.model_validate(bad)


def test_three_households_make_it_unsafe_one_person_cannot():
    spam = [wr("a", s, NOW - HOUR) for s in range(2, 12)]  # one person, ten reports
    assert status(spam)["R3"]["status"] == "watch" and status(spam)["R3"]["households"] == 1
    three = [wr(a, 2, NOW - HOUR, issue=i) for a, i in zip("abc", ("smell", "color", "smell"), strict=True)]
    s = status(three)["R3"]
    assert s["status"] == "unsafe" and s["issues"] == {"color": 1, "smell": 2}
    assert status(three)["R0"]["status"] == "ok"


def test_old_reports_expire_after_12_hours():
    old = [wr(a, 2, NOW - 13 * HOUR) for a in "abc"]
    assert status(old)["R3"]["status"] == "ok"


def test_links_illness_on_that_level_after_the_first_report():
    hr = lambda a, ts, lv: {"type": "health_report", "author": a, "seq": 2, "ts": ts,
                            "payload": {"level": lv, "sector": f"L{lv}-S01"}}
    recs = [*(wr(a, 2, NOW - 3 * HOUR) for a in "abc"),
            hr("x", NOW - HOUR, 3), hr("y", NOW - HOUR, 2), hr("z", NOW - 5 * HOUR, 3)]
    assert status(recs)["R3"]["illness_since"] == 1  # x only: y is another level, z came before


# --- in the sim -----------------------------------------------------------------------------

N = 80


@pytest.fixture(scope="module")
def world():
    return build_world()


def test_a_broken_recycler_gets_flagged_and_heal_fixes_it(world):
    sim = Sim(world, seed=3, n_residents=N)
    sim.queue_cmd({"cmd": "water", "level": 2, "ok": False})
    for _ in range(80):
        sim.tick()
    snap = sim.snapshot()
    assert snap["water_bad"] == [2]
    w = {x["id"]: x for x in snap["stats"]["water"]}
    assert w["R2"]["status"] == "unsafe"
    assert all(w[k]["status"] != "unsafe" for k in ("R0", "R1", "R3"))
    sim.queue_cmd({"cmd": "heal"})
    sim.tick()
    assert sim.snapshot()["water_bad"] == []


def test_each_resident_reports_a_failure_once(world):
    sim = Sim(world, seed=5, n_residents=N)
    sim.queue_cmd({"cmd": "water", "level": 1, "ok": False})
    for _ in range(150):
        sim.tick()
    for r in sim.residents:
        mine = [x for x in r["node"].all_records() if x["author"] == r["node"].node_id
                and x["type"] == "water_report" and x["payload"]["severity"] > 1]
        assert len(mine) <= 1


def test_bad_water_makes_people_sick(world):
    def water_cases(broken: bool) -> int:
        sim = Sim(world, seed=8, n_residents=N)
        if broken:
            sim.queue_cmd({"cmd": "water", "level": 0, "ok": False})
        for _ in range(150):
            sim.tick()
        return sum(c.cause == "water" for c in sim.sickness.cases)
    assert water_cases(False) == 0 and water_cases(True) > 0


def test_phone_water_report_and_bad_commands(world):
    sim = Sim(world, seed=3, n_residents=N, sickness=False)
    for bad in ({"issue": "fizzy", "severity": 1}, {"issue": "smell", "severity": 9},
                {"issue": "smell", "severity": True}, {"issue": ["smell"], "severity": 1}):
        sim._apply({"cmd": "water_report", "resident": 0, **bad})
    for bad in ({"level": 9, "ok": False}, {"level": True, "ok": False}, {"level": 1, "ok": "no"}):
        sim._apply({"cmd": "water", **bad})
    assert sim._to_send == [] and sim.water_bad == {}
    sim._apply({"cmd": "water_report", "resident": 0, "issue": "taste", "severity": 3})
    sim.tick()
    p = sim.phone(0)
    (e,) = [e for e in p["tabs"]["water_report"] if e["mine"]]
    lv = e["p"]["level"]
    assert e["p"]["recycler"] == f"R{lv}" and e["p"]["issue"] == "taste"
    assert {x["id"] for x in p["water"]} == {"R0", "R1", "R2", "R3"}
