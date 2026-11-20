"""H9: a simple lung sickness for Countertally to count (ground truth lives only here).

SIR per resident: healthy -> sick (for 1-3 days) -> recovered (immune for the run).
  * Deeper is worse: each level has its own base chance per tick (bad air, crowding).
  * It spreads in a room: every sick roommate raises the chance a little.
  * Most sick residents file one signed health_report from their phone, a few ticks
    after they fall ill. That report is the only thing Countertally ever sees.

The Directorate's official number is an undercount on purpose (Doc 2 risk register):
it hears about OFFICIAL_SHARE of cases, OFFICIAL_LAG ticks late.

All randomness comes from the sim's seeded rng, and nothing iterates a set.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import Sim

BASE_P = (0.0001, 0.0003, 0.0006, 0.002)  # per resident per tick, by level (L0..L3)
SPREAD = 0.004  # extra chance per tick x share of the room that is sick
SICK_TICKS = (144, 432)  # ill for 1-3 sim days
REPORT_P = 0.85  # share of sick households that file a report
REPORT_DELAY = (1, 18)  # ticks between falling ill and filing (up to 3 hours)
OFFICIAL_SHARE, OFFICIAL_LAG = 0.25, 72  # the Directorate: 25% of cases, 12 hours late
WINDOW_DAYS = 14  # health reports live 14 days (Doc 2 §1.2); cases count for as long

HOUSEHOLD = (1, 1, 2, 2, 3, 4)  # how many are sick at home, per case
SYMS = ("fever", "breath", "chest")  # cough is always one of them


@dataclass
class Case:
    resident: int
    onset: int  # tick
    onset_day: int
    level: int
    sector: str
    sick: int  # people sick in the household
    severity: int
    sym: list[str]
    ends: int  # tick they recover
    report_at: int | None  # tick they file, or None if they never do


class Sickness:
    def __init__(self) -> None:
        self.cases: list[Case] = []  # every case so far, in onset order
        self.had: dict[int, Case] = {}  # resident -> their case (sick now or recovered)

    def step(self, sim: Sim) -> None:
        """One tick: new infections, then queue the reports that are due."""
        t, rng = sim.t, sim.rng
        crowd: Counter[int] = Counter(r["sec"] for r in sim.residents)
        ill: Counter[int] = Counter(sim.residents[c.resident]["sec"]
                                    for c in self.cases if c.ends > t)
        for r in sim.residents:
            if r["id"] in self.had:
                continue
            p = BASE_P[sim.level_of[r["sec"]]] + SPREAD * ill[r["sec"]] / crowd[r["sec"]]
            if rng.random() < p:
                self._fall_ill(sim, r)
        for c in self.cases:
            if c.report_at == t:  # filed from where they fell ill
                sim._to_send.append((c.resident, "health_report", {
                    "level": c.level, "sector": c.sector, "sym": c.sym,
                    "severity": c.severity, "sick": c.sick, "onset_day": c.onset_day}))

    def _fall_ill(self, sim: Sim, r: dict) -> None:
        t, rng = sim.t, sim.rng
        sym = ["cough", *[s for s in SYMS if rng.random() < 0.4]]
        reports = rng.random() < REPORT_P
        c = Case(resident=r["id"], onset=t, onset_day=sim.day, level=sim.level_of[r["sec"]],
                 sector=sim.sectors[r["sec"]]["id"], sick=rng.choice(HOUSEHOLD),
                 severity=rng.choice((1, 1, 2, 2, 3)), sym=sym,
                 ends=t + rng.randint(*SICK_TICKS),
                 report_at=t + rng.randint(*REPORT_DELAY) if reports else None)
        self.cases.append(c)
        self.had[r["id"]] = c

    def sick_now(self, t: int) -> list[int]:
        return [c.resident for c in self.cases if c.ends > t]

    def truth(self, t: int, day: int, levels: int) -> list[int]:
        """Real cases per level in the window: what everyone would like to know."""
        out = [0] * levels
        for c in self.cases:
            if c.onset_day > day - WINDOW_DAYS:
                out[c.level] += c.sick
        return out

    def official(self, t: int, day: int, levels: int) -> list[int]:
        """The Directorate's number: a share of the cases it has heard about by now."""
        out = [0] * levels
        for c in self.cases:
            if c.onset <= t - OFFICIAL_LAG and c.onset_day > day - WINDOW_DAYS:
                out[c.level] += c.sick
        return [int(n * OFFICIAL_SHARE) for n in out]
