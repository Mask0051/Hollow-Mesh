"""P8 — Countertally: counting from health_reports, Sybil damping, k-anonymity, staleness."""

from __future__ import annotations

import math

from hollowmesh.services.countertally import DAY_MS, poisson_sf, tally

TICK = 600_000
NOW = 3 * DAY_MS  # day 3


def hr(author: str, seq: int, ts: int, *, sector: str = "L3-S04", sick: int = 1,
       onset: int = 3) -> dict:
    return {"type": "health_report", "author": author, "seq": seq, "ts": ts,
            "payload": {"level": int(sector[1]), "sector": sector, "sym": ["cough"],
                        "severity": 1, "sick": sick, "onset_day": onset}}


def run(records: list[dict]) -> dict:
    return tally(records, NOW, levels=4, n_sectors=33, tick_ms=TICK)


def test_sums_household_cases_per_level():
    out = run([hr("a", 2, NOW - TICK, sick=3), hr("b", 2, NOW - TICK, sector="L1-S02", sick=2)])
    assert out["countertally"] == [0, 2, 0, 3]
    assert out["reports"] == 2


def test_one_report_per_author_per_day_counts():
    # A Sybil-ish author spamming five reports in one day counts once (the latest).
    spam = [hr("a", s, NOW - TICK * (10 - s), sick=5, onset=3 - s % 2) for s in range(2, 7)]
    assert run(spam)["countertally"][3] == 5


def test_a_later_report_updates_the_same_episode():
    # Same household, same onset, reported on two days: the update replaces it.
    out = run([hr("a", 2, NOW - DAY_MS, sick=1, onset=2), hr("a", 3, NOW - TICK, sick=4, onset=2)])
    assert out["countertally"][3] == 4


def test_old_episodes_fall_out_of_the_14_day_window():
    late = tally([hr("a", 2, 0, onset=0)], 14 * DAY_MS, 4, 33, TICK)
    assert late["countertally"][3] == 0
    assert tally([hr("a", 2, 0, onset=0)], 13 * DAY_MS, 4, 33, TICK)["countertally"][3] == 1


def test_staleness_uses_any_record_from_that_level():
    post = {"type": "post", "author": "p", "seq": 2, "ts": NOW - 5 * TICK,
            "payload": {"text": "hi", "level": 1, "sector": "L1-S00"}}
    out = run([post, hr("a", 2, NOW - 40 * TICK)])
    assert out["staleness"] == [None, 5, None, 40]  # never heard from L0/L2: unknown, not 0


def test_hotspots_need_three_households_and_a_real_excess():
    two = [hr(a, 2, NOW - TICK, sick=10) for a in "ab"]
    assert run(two)["flags"] == []  # k-anonymity: 2 households is not enough to name a sector
    three = [hr(a, 2, NOW - TICK, sick=10) for a in "abc"]
    (f,) = run(three)["flags"]
    assert f["sector"] == "L3-S04" and f["cases"] == 30 and f["households"] == 3
    assert f["p"] < 0.01
    spread = [hr(a, 2, NOW - TICK, sector=f"L2-S0{i}") for i, a in enumerate("abcdefgh")]
    assert run(spread)["flags"] == []  # one case per sector is no outbreak


def test_poisson_tail():
    assert poisson_sf(0, 3.0) == 1.0
    assert math.isclose(poisson_sf(1, 2.0), 1 - math.exp(-2.0))
    assert poisson_sf(20, 1.0) < 1e-12
