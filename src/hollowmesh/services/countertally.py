"""P8 Countertally v1: count lung sickness from the health_reports one phone holds.

Input: the records in ONE node's store (any node: every phone can run this itself).
Output, per level: cases in the last 14 days and how stale that level's data is,
plus outbreak hotspots per sector.

Rules (Doc 2 §1.2, §2c, §3):
  * Sybil damping: at most one report per author per day counts (their latest that day).
  * One household episode per (author, onset_day): a later report updates the earlier one.
  * k-anonymity: a sector is only named (as a hotspot) once >= 3 different authors
    reported from it. Smaller sectors still count toward their level's total.
  * Staleness: time since the newest record of ANY type from that level. Missing data
    never reads as "0 cases": a stale level's number is a floor.
  * Hotspots: Poisson tail p of a sector's cases against the average sector.
"""

from __future__ import annotations

import math
from typing import Any

DAY_MS = 86_400_000
WINDOW_DAYS = 14
K_ANON = 3
HOTSPOT_P = 0.01
MAX_FLAGS = 5


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term = cdf = math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def tally(records: list[dict[str, Any]], now_ms: int, levels: int, n_sectors: int,
          tick_ms: int) -> dict[str, Any]:
    today = now_ms // DAY_MS
    newest: list[int | None] = [None] * levels  # newest ts heard from each level
    per_day: dict[tuple[str, int], dict[str, Any]] = {}  # (author, day sent) -> latest report

    for x in records:
        p = x["payload"]
        lv = p.get("level")
        if isinstance(lv, int) and 0 <= lv < levels and (newest[lv] is None or x["ts"] > newest[lv]):
            newest[lv] = x["ts"]
        if x["type"] != "health_report" or p["onset_day"] <= today - WINDOW_DAYS:
            continue
        k = (x["author"], x["ts"] // DAY_MS)
        if k not in per_day or x["seq"] > per_day[k]["seq"]:
            per_day[k] = x

    episodes: dict[tuple[str, int], dict[str, Any]] = {}
    for x in per_day.values():
        k = (x["author"], x["payload"]["onset_day"])
        if k not in episodes or x["seq"] > episodes[k]["seq"]:
            episodes[k] = x

    cases = [0] * levels
    cells: dict[str, list[Any]] = {}  # sector -> [cases, {author: None}]
    for x in sorted(episodes.values(), key=lambda x: (x["author"], x["seq"])):
        p = x["payload"]
        cases[p["level"]] += p["sick"]
        cell = cells.setdefault(p["sector"], [0, {}])
        cell[0] += p["sick"]
        cell[1][x["author"]] = None

    lam = max(1.0, sum(cases) / max(1, n_sectors))  # an average sector
    flags = []
    for sector, (n, authors) in sorted(cells.items()):
        if len(authors) >= K_ANON:
            p = poisson_sf(n, lam)
            if p < HOTSPOT_P:
                flags.append({"sector": sector, "cases": n, "households": len(authors),
                              "p": round(p, 6)})
    flags.sort(key=lambda f: (f["p"], f["sector"]))

    return {
        "countertally": cases,
        "staleness": [None if ts is None else max(0, (now_ms - ts) // tick_ms) for ts in newest],
        "flags": flags[:MAX_FLAGS],
        "reports": len(episodes),
    }
