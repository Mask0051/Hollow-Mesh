"""H2: the Hollow's floor plan (spec/world.json) — an irregular vault cross-section.

The shape is the story: it gets harder to reach the deeper you go.

    L0 GATE   8 rooms   ─┬── 3 stairwells
    L1 HOMES 10 rooms   ─┬── 2 stairwells (one per wing below)
    L2 FARMS  9 rooms, split into two wings by a collapsed tunnel
    L3 DEEP   6 rooms   ─┴── ONE service shaft up to the west wing

Stairwells don't line up, so a message (or a person) climbing out of the Deep has to cross
every level to reach the next stairwell. Cut the one shaft and the Deep is on its own.

Each stairwell has its own column `x`; it must sit inside both rooms it joins.
Sector ids use the payload format from Doc 2 §1.2: "L2-S07" (level 2, sector 7).

`python -m hollowmesh.sim.world` rewrites spec/world.json.
"""

from __future__ import annotations

import json
import pathlib

Y0, DY = 0.14, 0.24  # centre of level 0, distance between levels (fractions of height)
GAP, RUBBLE, LM, RM = 0.006, 0.03, 0.022, 0.978  # room gap, collapsed gap, left/right edge
MIN_OVERLAP = 0.045  # a stairwell column needs this much room above and below it

# (name, relative width) per room, west to east.
ROOMS = [
    [("Vault Door", 1.5), ("Airlock", .9), ("Security", 1.0), ("Armory", .8),
     ("Overseer", 1.3), ("Comms", .9), ("Lobby", 1.4), ("Exit Tunnel", 1.1)],
    [("Dorm A", 1.0), ("Stair Hall", 1.1), ("Mess Hall", 1.3), ("Kitchen", .8), ("Clinic", .9),
     ("Rec Room", 1.2), ("Dorm B", 1.0), ("Laundry", .8), ("Dorm C", 1.0), ("Storage", .9)],
    [("Hydroponics", 1.3), ("Pump Hall", 1.0), ("Garden A", 1.2), ("Seed Bank", .9),
     ("Garden B", 1.2), ("Workshop", .9), ("Forge", 1.0), ("Garden C", 1.1), ("Recycler", 1.0)],
    [("Coolant", 1.0), ("Service Shaft", 1.6), ("Reactor", 1.0), ("Generators", 1.3),
     ("Mine Face", 1.4), ("Vent Hub", 1.0)],
]
LEVELS = len(ROOMS)
COLLAPSED = {(2, 3)}  # (level, i): no tunnel between rooms i and i+1 -> L2 has two wings
# (upper level, room above, room below, letter). Edge id: ST-{lv}-{lv+1}-{letter}.
STAIRS = [
    (0, 2, 2, "a"), (0, 4, 5, "b"), (0, 6, 8, "c"),
    (1, 0, 0, "a"), (1, 7, 6, "b"),  # one into each Farms wing
    (2, 2, 1, "a"),  # the only way in or out of the Deep
]


def sector_id(level: int, i: int) -> str:
    return f"L{level}-S{i:02d}"


def _rooms(level: int) -> list[tuple[float, float]]:
    """(centre x, width) of each room on a level, filling LM..RM."""
    rooms = ROOMS[level]
    gaps = [RUBBLE if (level, i) in COLLAPSED else GAP for i in range(len(rooms) - 1)]
    scale = (RM - LM - sum(gaps)) / sum(w for _, w in rooms)
    x, out = LM, []
    for i, (_, w) in enumerate(rooms):
        out.append((x + w * scale / 2, w * scale))
        x += w * scale + (gaps[i] if i < len(gaps) else 0)
    return out


def build_world() -> dict:
    sectors, edges = [], []
    lay = [_rooms(lv) for lv in range(LEVELS)]
    for lv, rooms in enumerate(lay):
        for i, (cx, wv) in enumerate(rooms):
            sectors.append({"id": sector_id(lv, i), "level": lv, "x": round(cx, 4),
                            "y": round(Y0 + DY * lv, 3), "w": round(wv, 4),
                            "name": ROOMS[lv][i][0]})
        for i in range(len(rooms) - 1):
            if (lv, i) not in COLLAPSED:
                a, b = sector_id(lv, i), sector_id(lv, i + 1)
                edges.append({"id": f"T-{a}-{b}", "a": a, "b": b, "kind": "tunnel"})
    for n, (lv, up, down, k) in enumerate(STAIRS):
        (ux, uw), (dx, dw) = lay[lv][up], lay[lv + 1][down]
        lo, hi = max(ux - uw / 2, dx - dw / 2), min(ux + uw / 2, dx + dw / 2)
        assert hi - lo >= MIN_OVERLAP, f"ST-{lv}-{lv + 1}-{k}: rooms barely overlap"
        edges.append({"id": f"ST-{lv}-{lv + 1}-{k}", "a": sector_id(lv, up),
                      "b": sector_id(lv + 1, down), "kind": "stairwell",
                      "x": round((lo + hi) / 2, 4), "dir": 1 if n % 2 else -1})
    return {"levels": LEVELS, "sectors": sectors, "edges": edges}


if __name__ == "__main__":
    p = pathlib.Path(__file__).resolve().parents[3] / "spec" / "world.json"
    p.write_text(json.dumps(build_world(), indent=1) + "\n")
    print("wrote", p)
