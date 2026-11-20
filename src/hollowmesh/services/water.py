"""Water watch (prompt #4: catch bad water before people drink it).

Reads water_reports (and health_reports, for the link to illness) from ONE phone's
store, and says per recycler:
  * ok      no reports in the last 12 h
  * watch   1-2 households reported something (could be one person's bad day)
  * unsafe  3+ different households reported in the last 12 h: don't drink it

Rules:
  * One household = one author: a person reporting ten times still counts once, so one
    phone can't raise the alarm alone (the same damping as Countertally).
  * illness_since: households on that level that filed a health report after the first
    water report in the window. It links a sickness cluster to the water upstream.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

WINDOW_MS = 12 * 3_600_000
UNSAFE_HOUSEHOLDS = 3


def water_status(records: list[dict[str, Any]], now_ms: int, recyclers: list[dict[str, Any]],
                 tick_ms: int) -> list[dict[str, Any]]:
    since = now_ms - WINDOW_MS
    latest: dict[tuple[str, str], dict[str, Any]] = {}  # (recycler, author) -> newest report
    health: list[dict[str, Any]] = []
    for x in records:
        if x["ts"] < since:
            continue
        if x["type"] == "water_report":
            k = (x["payload"]["recycler"], x["author"])
            if k not in latest or x["seq"] > latest[k]["seq"]:
                latest[k] = x
        elif x["type"] == "health_report":
            health.append(x)

    out = []
    for rc in recyclers:
        reps = [x for (rid, _), x in sorted(latest.items()) if rid == rc["id"]]
        n = len(reps)
        status = "unsafe" if n >= UNSAFE_HOUSEHOLDS else "watch" if n else "ok"
        first = min((x["ts"] for x in reps), default=None)
        ill = ({x["author"]: None for x in health
                if x["payload"]["level"] == rc["level"] and x["ts"] >= first}
               if first is not None else {})
        issues = Counter(x["payload"]["issue"] for x in reps)
        last = max((x["ts"] for x in reps), default=None)
        out.append({"id": rc["id"], "name": rc["name"], "level": rc["level"],
                    "sector": rc["sector"], "status": status, "households": n,
                    "issues": dict(sorted(issues.items())),
                    "worst": max((x["payload"]["severity"] for x in reps), default=0),
                    "last_tick": None if last is None else last // tick_ms,
                    "illness_since": len(ill)})
    return out
