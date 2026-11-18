"""H2: the Hollow's floor plan (spec/world.json) — an irregular vault cross-section.

Rooms have different widths and names per level. The three stairwells are in the same
columns on every level so the staircases line up; their directions alternate.
Sector ids use the payload format from Doc 2 §1.2: "L2-S07" (level 2, sector 7).

`python -m hollowmesh.sim.world` rewrites spec/world.json.
"""

from __future__ import annotations

import json
import pathlib
import random

LEVELS, PER_LEVEL = 4, 10
STAIRS = {"a": (1, 0.17, +1), "b": (5, 0.50, -1), "c": (8, 0.80, +1)}  # sector idx, x column, direction
GAP, LM, RM = 0.006, 0.022, 0.978
NAMES = [
    ["Vault Door", "Airlock", "Security", "Armory", "Guard Post", "Overseer", "Comms", "Lobby", "Archive", "Exit Tunnel"],
    ["Dorm A", "Stair Hall", "Mess Hall", "Kitchen", "Clinic", "Rec Room", "Dorm B", "Laundry", "Dorm C", "Storage"],
    ["Hydroponics", "Pump Hall", "Workshop", "Garden A", "Garden B", "Seed Bank", "Forge", "Garden C", "Recycler", "Tool Room"],
    ["Reactor", "Coolant", "Pump Room", "Generators", "Mine Shaft", "Ore Store", "Control", "Boiler", "Waste Tank", "Vent Hub"],
]


def sector_id(level: int, i: int) -> str:
    return f"L{level}-S{i:02d}"


def _split(rng, start, end, n):
    avail = end - start - GAP * (n - 1)
    parts = [rng.uniform(0.8, 1.25) for _ in range(n)]
    tot, x, out = sum(parts), start, []
    for p in parts:
        wv = avail * p / tot
        out.append((x + wv / 2, wv))
        x += wv + GAP
    return out


def _layout(rng):
    (i1, c1, _), (i5, c5, _), (i8, c8, _) = STAIRS["a"], STAIRS["b"], STAIRS["c"]
    w1, w5, w8 = (rng.uniform(0.08, 0.105) for _ in range(3))
    out = [None] * PER_LEVEL
    out[i1], out[i5], out[i8] = (c1, w1), (c5, w5), (c8, w8)
    out[0] = _split(rng, LM, c1 - w1 / 2 - GAP, 1)[0]
    out[2:5] = _split(rng, c1 + w1 / 2 + GAP, c5 - w5 / 2 - GAP, 3)
    out[6:8] = _split(rng, c5 + w5 / 2 + GAP, c8 - w8 / 2 - GAP, 2)
    out[9] = _split(rng, c8 + w8 / 2 + GAP, RM, 1)[0]
    return out


def build_world() -> dict:
    rng = random.Random(11)  # fixed seed -> same map every run (separate from the sim seed)
    sectors, edges = [], []
    for lv in range(LEVELS):
        lay = _layout(rng)
        for i, (cx, wv) in enumerate(lay):
            sectors.append({"id": sector_id(lv, i), "level": lv, "x": round(cx, 4),
                            "y": round(0.14 + 0.24 * lv, 3), "w": round(wv, 4),
                            "name": NAMES[lv][i]})
        for i in range(PER_LEVEL - 1):
            a, b = sector_id(lv, i), sector_id(lv, i + 1)
            edges.append({"id": f"T-{a}-{b}", "a": a, "b": b, "kind": "tunnel"})
    for lv in range(LEVELS - 1):
        for k, (i, _, d) in STAIRS.items():
            edges.append({"id": f"ST-{lv}-{lv + 1}-{k}", "a": sector_id(lv, i),
                          "b": sector_id(lv + 1, i), "kind": "stairwell", "dir": d})
    return {"levels": LEVELS, "sectors": sectors, "edges": edges}


if __name__ == "__main__":
    p = pathlib.Path(__file__).resolve().parents[3] / "spec" / "world.json"
    p.write_text(json.dumps(build_world(), indent=1) + "\n")
    print("wrote", p)
