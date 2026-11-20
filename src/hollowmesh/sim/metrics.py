"""P9: measured results -> results.json (`make metrics`).

Runs the real protocol headless (no dashboard), over many seeds, and writes what happened.
Same seeds = same numbers, byte for byte: no wall-clock time goes into the file.

    1. delivery   An SOS from the Deep: what share of working phones get it within a day,
                  and how fast, as more phones die (Wi-Fi cut, mesh only).
    2. heal       Split the Hollow (Wi-Fi + every stairwell cut) for 12 h while people keep
                  posting, then reconnect: how long until every phone holds everything
                  that existed at the moment of the heal.
    3. tamper     Lying relays on every stairwell: how many altered copies they sent, and how
                  many any phone accepted.
    4. water      The Deep's recycler breaks (Wi-Fi cut): how long until a phone on another
                  level marks it unsafe, and how often a healthy recycler gets marked.
    5. countertally  Two sim days of lung sickness: the Directorate's number and
                  Countertally's (on one phone), as a share of the real case count.

1 tick = 10 sim-minutes. `python -m hollowmesh.sim.metrics --help` for options.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from .runner import TICK_MS, Sim
from .world import build_world

RESIDENTS = 300
SEEDS = 10
DAY = 144  # ticks
SPLIT = 72  # ticks the Hollow stays split (12 h)
MIN_PER_TICK = TICK_MS // 60_000

_WORLD = build_world()


def _holds(r: dict[str, Any], author: str, seq: int) -> bool:
    return r["node"].version_vector().get(author, 0) >= seq


def _sos_from_deep(sim: Sim, alive: list[bool]) -> tuple[str, int]:
    """A working phone on level 3 sends an SOS (the dashboard's "SOS from Deep")."""
    r = next(r for r in sim.residents if alive[r["id"]] and sim._level(r) == 3)
    rec = sim._publish(r, "help_request", {
        "kind": "air", "level": 3, "sector": sim.sectors[r["sec"]]["id"], "urgency": 3,
        "people": 12, "text": "Urgent: air failing"})
    return rec["author"], rec["seq"]


# ---- 1. delivery -----------------------------------------------------------------------------

def delivery(seed: int, wifi: bool, off: float, deep_dark: bool = False) -> dict[str, Any]:
    sim = Sim(_WORLD, seed=seed, n_residents=RESIDENTS, sickness=False)
    for _ in range(5):  # settle: profiles spread
        sim.tick()
    pick = random.Random(seed * 7919 + int(off * 100))  # which phones die: own rng
    dead = pick.sample(range(RESIDENTS), int(off * RESIDENTS))
    for i in dead:
        sim.phones_off[i] = True
    sim.net_cut = not wifi
    if deep_dark:
        sim.dark = [3]
    alive = [not x for x in sim.phones_off]
    n_alive = sum(alive)
    author, seq = _sos_from_deep(sim, alive)
    t50 = t95 = t_all = t_levels = None
    share = 0.0
    for t in range(1, DAY + 1):
        sim.tick()
        have = [r for r in sim.residents if alive[r["id"]] and _holds(r, author, seq)]
        share = len(have) / n_alive
        levels = len({sim._level(r) for r in have})
        if t_levels is None and levels == sim.levels:
            t_levels = t
        if t50 is None and share >= 0.5:
            t50 = t
        if t95 is None and share >= 0.95:
            t95 = t
        if share == 1.0:
            t_all = t
            break
    return {"delivered": share, "t50": t50, "t95": t95, "t_all": t_all, "t_levels": t_levels}


# ---- 2. partition, then heal -------------------------------------------------------------------

def heal(seed: int) -> dict[str, Any]:
    sim = Sim(_WORLD, seed=seed, n_residents=RESIDENTS)
    for _ in range(5):
        sim.tick()
    sim.net_cut = True
    for e in sim.edges:
        if e["kind"] == "stairwell":
            e["up"] = False
    before = sim.total_published
    for _ in range(SPLIT):
        sim.tick()
    split_sync = sim.snapshot()["syncPct"]
    target: dict[str, int] = {}  # everything that exists anywhere at the moment of the heal
    for r in sim.residents:
        for a, n in r["node"].version_vector().items():
            target[a] = max(target.get(a, 0), n)
    sim.queue_cmd({"cmd": "heal"})
    t_conv = None
    for t in range(1, DAY + 1):
        sim.tick()
        if all(r["node"].version_vector().get(a, 0) >= n
               for r in sim.residents for a, n in target.items()):
            t_conv = t
            break
    return {"published_during_split": sim.total_published - before,
            "sync_at_heal": split_sync, "t_converge": t_conv,
            "records_at_heal": sum(target.values())}


# ---- 3. tamper ------------------------------------------------------------------------------

def tamper(seed: int) -> dict[str, Any]:
    sim = Sim(_WORLD, seed=seed, n_residents=RESIDENTS)
    sim.net_cut = True  # all traffic between levels has to cross a lying stairwell
    for e in sim.edges:
        if e["kind"] == "stairwell":
            sim.lying.append(e["id"])
    sim._to_inject.append("L3-S04")
    for _ in range(DAY // 2):
        sim.tick()
    accepted = sum(1 for r in sim.residents for x in r["node"].all_records()
                   if "[altered]" in str(x["payload"].get("text", "")))
    tamper_alerts = sum(1 for k in sim.alerted if not k.endswith(":fork"))
    return {"altered_sent": sim.altered_sent, "altered_accepted": accepted,
            "tampered_records_flagged": tamper_alerts}


# ---- 4. water ------------------------------------------------------------------------------

def water(seed: int) -> dict[str, Any]:
    sim = Sim(_WORLD, seed=seed, n_residents=RESIDENTS)
    sim.net_cut = True
    obs = next(r["id"] for r in sim.residents if sim._level(r) == 0)  # counted at the Gate
    sim.observer = obs
    for _ in range(5):
        sim.tick()
    sim.water_bad[3] = sim.t
    flagged = None
    false_alarm_ticks = 0
    for t in range(1, DAY + 1):
        sim.tick()
        w = {x["id"]: x["status"] for x in sim.stats()["water"]}
        if flagged is None and w["R3"] == "unsafe":
            flagged = t
        false_alarm_ticks += sum(w[k] == "unsafe" for k in ("R0", "R1", "R2"))
    return {"t_flagged": flagged, "false_unsafe_ticks": false_alarm_ticks}


# ---- 5. Countertally ---------------------------------------------------------------------------

def countertally(seed: int, wifi: bool) -> dict[str, Any]:
    sim = Sim(_WORLD, seed=seed, n_residents=RESIDENTS)
    sim.net_cut = not wifi
    first_ct = first_off = None
    trace: dict[str, list[int]] = {"hour": [], "truth": [], "countertally": [], "official": []}
    for t in range(1, 2 * DAY + 1):
        sim.tick()
        if t % 6 == 0:  # hourly, like Doc 2's analytics loop
            st = sim.stats()
            trace["hour"].append(t // 6)
            for k in ("truth", "countertally", "official"):
                trace[k].append(sum(st[k]))
            if first_ct is None and sum(st["countertally"]) >= 10:
                first_ct = t
            if first_off is None and sum(st["official"]) >= 10:
                first_off = t
    st = sim.stats()
    truth = sum(st["truth"])
    return {"truth": truth, "official": sum(st["official"]), "countertally": sum(st["countertally"]),
            "official_share": sum(st["official"]) / truth if truth else None,
            "countertally_share": sum(st["countertally"]) / truth if truth else None,
            "deep_truth": st["truth"][3], "deep_official": st["official"][3],
            "deep_countertally": st["countertally"][3],
            "t_first_10_countertally": first_ct, "t_first_10_official": first_off,
            "trace": trace}


# ---- summary ----------------------------------------------------------------------------------

def _summ(values: list[Any], minutes: bool = False) -> dict[str, Any]:
    """mean / min / max over runs; None (never happened) is reported, not averaged away."""
    got = [v for v in values if v is not None]
    out: dict[str, Any] = {"runs": len(values), "never": len(values) - len(got)}
    if got:
        k = MIN_PER_TICK if minutes else 1
        out |= {"mean": round(statistics.fmean(got) * k, 3), "median": round(statistics.median(got) * k, 3),
                "min": round(min(got) * k, 3), "max": round(max(got) * k, 3)}
    return out


def _call(job: tuple[str, tuple[Any, ...]]) -> dict[str, Any]:
    fn = {"delivery": delivery, "heal": heal, "tamper": tamper, "water": water,
          "countertally": countertally}
    return fn[job[0]](*job[1])


DELIVERY = [  # (name, wifi, share of phones dead, deep level blacked out)
    ("wifi_on", True, 0.0, False),
    ("mesh_only", False, 0.0, False),
    ("mesh_only_25pct_phones_dead", False, 0.25, False),
    ("mesh_only_50pct_phones_dead", False, 0.5, False),
    ("mesh_only_75pct_phones_dead", False, 0.75, False),
    ("mesh_only_deep_blackout", False, 0.0, True),
]


def run_all(seeds: int, workers: int | None = None) -> dict[str, Any]:
    ss = list(range(1, seeds + 1))
    jobs: list[tuple[str, tuple[Any, ...]]] = []
    jobs += [("delivery", (s, w, off, dd)) for _, w, off, dd in DELIVERY for s in ss]
    jobs += [("heal", (s,)) for s in ss]
    jobs += [("tamper", (s,)) for s in ss]
    jobs += [("water", (s,)) for s in ss]
    jobs += [("countertally", (s, w)) for w in (True, False) for s in ss]
    with ProcessPoolExecutor(workers) as pool:
        res = list(pool.map(_call, jobs))  # results come back in job order: deterministic
    it = iter(res)

    out: dict[str, Any] = {
        "about": {"command": "make metrics", "residents": RESIDENTS, "seeds": ss,
                  "tick_minutes": MIN_PER_TICK, "time_units": "sim-minutes",
                  "note": "Real protocol (signed feeds, verified ingest, gossip); the sim decides "
                          "only who can reach whom. Same seeds give the same file."},
        "delivery": {}, "heal": {}, "tamper": {}, "water": {}, "countertally": {}}
    for name, w, off, dd in DELIVERY:
        runs = [next(it) for _ in ss]
        out["delivery"][name] = {
            "wifi": w, "phones_dead": off, "deep_blackout": dd,
            "delivered_share_within_1_day": _summ([r["delivered"] for r in runs]),
            "minutes_to_all_4_levels": _summ([r["t_levels"] for r in runs], minutes=True),
            "minutes_to_50pct": _summ([r["t50"] for r in runs], minutes=True),
            "minutes_to_95pct": _summ([r["t95"] for r in runs], minutes=True),
        }
    runs = [next(it) for _ in ss]
    out["heal"] = {"split_minutes": SPLIT * MIN_PER_TICK,
                   "sync_at_heal": _summ([r["sync_at_heal"] for r in runs]),
                   "records_at_heal": _summ([r["records_at_heal"] for r in runs]),
                   "minutes_to_converge": _summ([r["t_converge"] for r in runs], minutes=True),
                   "converged_runs": sum(r["t_converge"] is not None for r in runs)}
    runs = [next(it) for _ in ss]
    out["tamper"] = {"altered_copies_sent": sum(r["altered_sent"] for r in runs),
                     "altered_copies_accepted": sum(r["altered_accepted"] for r in runs),
                     "tampered_records_flagged": sum(r["tampered_records_flagged"] for r in runs)}
    runs = [next(it) for _ in ss]
    out["water"] = {"setup": "Deep recycler breaks, Wi-Fi cut, counted on a phone on level 0",
                    "minutes_to_unsafe": _summ([r["t_flagged"] for r in runs], minutes=True),
                    "healthy_recycler_marked_unsafe_minutes": sum(r["false_unsafe_ticks"]
                                                                  for r in runs) * MIN_PER_TICK}
    for w in (True, False):
        runs = [next(it) for _ in ss]
        out["countertally"]["wifi_on" if w else "network_cut"] = {
            "sim_days": 2,
            "official_share_of_truth": _summ([r["official_share"] for r in runs]),
            "countertally_share_of_truth": _summ([r["countertally_share"] for r in runs]),
            "deep_level_share": {
                "official": _summ([r["deep_official"] / r["deep_truth"] if r["deep_truth"] else None
                                   for r in runs]),
                "countertally": _summ([r["deep_countertally"] / r["deep_truth"]
                                       if r["deep_truth"] else None for r in runs])},
            "minutes_until_10_cases_seen": {
                "countertally": _summ([r["t_first_10_countertally"] for r in runs], minutes=True),
                "official": _summ([r["t_first_10_official"] for r in runs], minutes=True)},
            # one run, hour by hour (total cases, all levels): the README's outbreak chart
            "trace_seed": ss[0], "trace": runs[0]["trace"],
        }
    return out


def _pct(x: dict[str, Any]) -> str:
    return f"{x['mean'] * 100:.0f}%" if "mean" in x else "n/a"


def _min(x: dict[str, Any]) -> str:
    if "mean" not in x:
        return "never"
    m = x["median"]
    s = f"{m / 60:.1f} h" if m >= 120 else f"{m:.0f} min"
    return s + (f" ({x['never']} never)" if x["never"] else "")


def summary(r: dict[str, Any]) -> str:
    lines = [(f"Rat King metrics: {r['about']['residents']} residents, "
              f"{len(r['about']['seeds'])} seeds (medians of time, means of shares)"), "",
             "SOS from the Deep             delivered/1 day   all 4 levels   95% of phones"]
    for name, d in r["delivery"].items():
        lines.append(f"  {name:29s} {_pct(d['delivered_share_within_1_day']):>10s}   "
                     f"{_min(d['minutes_to_all_4_levels']):>12s}   {_min(d['minutes_to_95pct']):>12s}")
    h = r["heal"]
    lines += ["", (f"Split 12 h, then heal: phones held {_pct(h['sync_at_heal'])} of all records at the "
                   f"heal; everyone had everything after {_min(h['minutes_to_converge'])} "
                   f"({h['converged_runs']}/{h['minutes_to_converge']['runs']} runs converged)")]
    t = r["tamper"]
    lines += [(f"Lying relays: {t['altered_copies_sent']} altered copies sent, "
               f"{t['altered_copies_accepted']} accepted, {t['tampered_records_flagged']} records flagged")]
    wt = r["water"]
    lines += [(f"Water: the Deep's broken recycler marked unsafe on a Gate phone after "
               f"{_min(wt['minutes_to_unsafe'])}; healthy recyclers marked unsafe for "
               f"{wt['healthy_recycler_marked_unsafe_minutes']} min in total")]
    for k, c in r["countertally"].items():
        lines += [(f"Countertally ({k}): sees {_pct(c['countertally_share_of_truth'])} of real cases, "
                   f"Directorate {_pct(c['official_share_of_truth'])}; first 10 cases seen after "
                   f"{_min(c['minutes_until_10_cases_seen']['countertally'])} vs "
                   f"{_min(c['minutes_until_10_cases_seen']['official'])}")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m hollowmesh.sim.metrics", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds", type=int, default=SEEDS)
    p.add_argument("--out", default="results.json")
    p.add_argument("--workers", type=int, default=None, help="processes (default: all cores)")
    a = p.parse_args(argv)
    res = run_all(a.seeds, a.workers)
    pathlib.Path(a.out).write_text(json.dumps(res, indent=1) + "\n")
    print(summary(res))
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
