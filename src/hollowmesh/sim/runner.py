"""H4, H5, H10 + P6: the simulated Hollow, running on the real protocol.

Every resident carries a real Node (signed feeds, verified ingest) and a Gossiper that
syncs over a MemoryTransport. The sim decides only WHO CAN REACH WHOM each tick; the
protocol does everything else (Doc 2 §1.6).

Tick loop (1 tick = 10 sim-minutes):
    1. apply queued / scripted commands (Directorate attacks, injects)
    2. residents walk (only through intact edges)
    3. reachability: who can talk to whom this tick
    4. publish: injected reports + random posts (pushed to up to 3 reachable peers)
    5. every node gossips with up to 2 reachable peers, in a seeded shuffled order
    6. snapshot for the dashboard

Reachability rules (Doc 2 §1.6):
    * Mesh: same sector, each node links to <=3 sampled neighbours. Each intact tunnel
      or stairwell links one random pair across it.
    * Stairwells are the only links between levels. A cut stairwell carries no links
      and no movement.
    * Directorate network: while it's up, every node gets 1 random long-range link per
      tick. Cutting it leaves only the mesh plus people walking.
    * Blackout: nodes on that level take part with probability 0.3 per tick.
    * Lying relay (P7): records crossing a lying edge get their payload rewritten in
      transit. The signature no longer matches, so the receiving node raises a tamper alert.

Deterministic: one seeded random.Random for everything (keys, walking, links, gossip),
and no set is ever iterated. Same seed + same script = same run.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from ..protocol.node import Alert, Node
from ..protocol.record import Keypair, Record, RecordProblem, check_record
from ..protocol.sync import Gossiper
from ..protocol.transport.memory import MemoryTransport

TICK_MS = 600_000  # 1 tick = 10 sim-minutes; record ts is sim time, never wall time
WALK_P, POST_P = 0.08, 0.001
SECTOR_LINKS = 3  # mesh: each node links to <=3 others in its sector
BLACKOUT_ON_P = 0.3  # a node on a dark level is up this tick with this probability
MAX_PARTICLES = 60  # cross-sector message particles per snapshot
SHOWN_ALERTS = ("tamper", "fork")  # gaps are routine during gossip; counted, not shown

DEMO_SCRIPT: dict[int, list[dict[str, Any]]] = {  # tick -> commands (H10)
    10: [{"cmd": "cut_network"}],
    12: [{"cmd": "inject", "sector": "L3-S04"}],
    60: [{"cmd": "cut_stairwell", "id": "ST-1-2-b"}],
    100: [{"cmd": "blackout", "level": 2}],
    140: [{"cmd": "heal"}],
    160: [{"cmd": "tamper", "id": "ST-0-1-b"}, {"cmd": "inject", "sector": "L1-S05"}],
    220: [{"cmd": "heal"}],
}


class VerifyCache:
    """check_record, remembered per record object (Doc 2 §1.3's global verify cache).

    Records travel by reference in the sim, so one honest record is the same object on
    all 300 nodes and only needs verifying once. A tampered copy is a new object, so it
    is always checked fresh. We keep a reference to every record we've seen so its
    id() can't be reused by another object. Nodes never modify records.
    """

    def __init__(self) -> None:
        self._seen: dict[int, tuple[Any, RecordProblem | None]] = {}

    def __call__(self, record: Any) -> RecordProblem | None:
        hit = self._seen.get(id(record))
        if hit is not None and hit[0] is record:
            return hit[1]
        problem = check_record(record)
        self._seen[id(record)] = (record, problem)
        return problem


def _altered(record: Record) -> Record:
    """What the lying relay forwards: same id and signature, rewritten payload."""
    payload = record["payload"]
    text = payload.get("text")
    payload = {**payload, "text": (text if isinstance(text, str) else "") + " [altered]"}
    return {**record, "payload": payload}  # a copy; the shared original is untouched


class Sim:
    """The simulated Hollow. Also the `Network` that every MemoryTransport asks."""

    def __init__(
        self,
        world: dict[str, Any],
        seed: int = 42,
        n_residents: int = 300,
        script: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.levels: int = world["levels"]
        self.sectors = world["sectors"]
        self.idx = {s["id"]: i for i, s in enumerate(self.sectors)}
        self.level_of = [s["level"] for s in self.sectors]
        self.edges = [dict(e, up=True) for e in world["edges"]]
        self.eidx = {e["id"]: i for i, e in enumerate(self.edges)}
        self.adj: list[list[tuple[int, int]]] = [[] for _ in self.sectors]
        for ei, e in enumerate(self.edges):
            a, b = self.idx[e["a"]], self.idx[e["b"]]
            self.adj[a].append((ei, b))
            self.adj[b].append((ei, a))

        self.script = script or {}
        self.t = 0
        self.queue: list[dict[str, Any]] = []
        self.net_cut = False
        self.dark: list[int] = []
        self.lying: list[str] = []  # edge ids with a lying relay on them

        self.total_published = 0
        self.probe: tuple[str, int] | None = None  # (author, seq) of the latest inject
        self.alerted: dict[str, None] = {}  # records already alerted on (ordered set)
        self.gap_alerts = 0
        self.events: list[dict[str, Any]] = []
        self.msgs: list[list[int]] = []
        self._to_inject: list[str] = []

        self._links: dict[str, list[str]] = {}  # node_id -> reachable node_ids this tick
        self._via: dict[tuple[str, str], int] = {}  # (a, b) -> edge index the link runs over

        verify = VerifyCache()
        self.residents: list[dict[str, Any]] = []
        self.by_node: dict[str, dict[str, Any]] = {}
        for i in range(n_residents):
            keys = Keypair.from_seed(self.rng.randbytes(32))  # seeded, so ids repeat per seed
            node = Node(keys, on_event=lambda a, i=i: self._on_event(i, a), check=verify)
            r = {"id": i, "sec": self.rng.randrange(len(self.sectors)), "node": node,
                 "jx": self.rng.uniform(-.014, .014), "jy": self.rng.uniform(-.05, .05)}
            r["gossip"] = Gossiper(node, MemoryTransport(node.node_id, self, self._relay), self.rng)
            self.residents.append(r)
            self.by_node[node.node_id] = r

        for r in self.residents:  # every feed starts with a profile (seq 1)
            self._publish(r, "profile", {"kind": "public", "level": self._level(r)})

    # ---- Network (what MemoryTransport asks) ----------------------------------------------

    def reachable(self, node_id: str) -> list[str]:
        return self._links.get(node_id, [])

    def node(self, node_id: str) -> Node:
        return self.by_node[node_id]["node"]

    # ---- commands (H5/H7: the control panel sends these) ----------------------------------

    def queue_cmd(self, c: Any) -> None:
        if isinstance(c, dict):
            self.queue.append(c)

    def _apply(self, c: dict[str, Any]) -> None:
        # Commands come from the browser, so anything malformed is ignored, never raised.
        # Doc 2's names (cut_net, cut_stair, deploy_relay) work too.
        k = c.get("cmd")
        if k in ("cut_network", "cut_net"):
            self.net_cut = True
        elif k == "blackout":
            lv = c.get("level")
            if isinstance(lv, int) and 0 <= lv < self.levels and lv not in self.dark:
                self.dark.append(lv)
        elif k in ("cut_stairwell", "cut_stair"):
            ei = self.eidx.get(c.get("id") or c.get("edge"))
            if ei is None:  # default: first stairwell still up
                ei = next((i for i, e in enumerate(self.edges)
                           if e["kind"] == "stairwell" and e["up"]), None)
            if ei is not None:
                self.edges[ei]["up"] = False
        elif k in ("tamper", "deploy_relay"):
            eid = c.get("id") or c.get("edge")
            if eid not in self.eidx:  # default: first stairwell
                eid = next(e["id"] for e in self.edges if e["kind"] == "stairwell")
            if eid not in self.lying:
                self.lying.append(eid)
        elif k == "inject":
            sector = c.get("sector", "L3-S04")
            if sector in self.idx:
                self._to_inject.append(sector)  # published after this tick's reachability
        elif k == "heal":
            self.net_cut, self.dark, self.lying = False, [], []
            for e in self.edges:
                e["up"] = True

    # ---- one tick ------------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        self.t += 1
        self.events, self.msgs = [], []
        self.queue += self.script.get(self.t, [])
        while self.queue:
            self._apply(self.queue.pop(0))

        for r in self.residents:  # walk (only through intact edges)
            if self.rng.random() < WALK_P:
                opts = [b for ei, b in self.adj[r["sec"]] if self.edges[ei]["up"]]
                if opts:
                    r["sec"] = self.rng.choice(opts)

        self._reachability()

        for sector in self._to_inject:
            self._inject(sector)
        self._to_inject = []
        for r in self.residents:
            if self.rng.random() < POST_P:
                self._publish(r, "post", {"text": f"post t{self.t}", "level": self._level(r),
                                          "sector": self.sectors[r["sec"]]["id"]})

        order = self.residents[:]
        self.rng.shuffle(order)
        for r in order:
            r["gossip"].gossip_round()
        return self.snapshot()

    def _reachability(self) -> None:
        links: dict[str, dict[str, None]] = {r["node"].node_id: {} for r in self.residents}
        via: dict[tuple[str, str], int] = {}

        def link(a: str, b: str, edge: int | None = None) -> None:
            if a == b:
                return
            links[a][b] = None
            links[b][a] = None
            if edge is not None:
                via[(a, b)] = via[(b, a)] = edge

        # Who's up: everyone, except dark levels where batteries are dying.
        on = [r for r in self.residents
              if self.level_of[r["sec"]] not in self.dark or self.rng.random() < BLACKOUT_ON_P]
        by: dict[int, list[str]] = defaultdict(list)
        for r in on:
            by[r["sec"]].append(r["node"].node_id)

        for s in range(len(self.sectors)):  # mesh within a sector
            group = by.get(s, [])
            for i, a in enumerate(group):
                others = group[:i] + group[i + 1:]
                for b in self.rng.sample(others, min(SECTOR_LINKS, len(others))):
                    link(a, b)

        for ei, e in enumerate(self.edges):  # one relay link across each intact edge
            ga, gb = by.get(self.idx[e["a"]]), by.get(self.idx[e["b"]])
            if e["up"] and ga and gb:
                link(self.rng.choice(ga), self.rng.choice(gb), ei)

        if not self.net_cut:  # Directorate network: 1 long-range link per node
            ids = [r["node"].node_id for r in on]
            for a in ids:
                link(a, self.rng.choice(ids))

        self._links = {a: list(bs) for a, bs in links.items()}
        self._via = via

    def _relay(self, sender: str, receiver: str, records: list[Record]) -> list[Record]:
        """P7: every delivery passes through here (MemoryTransport's relay hook)."""
        if not records:
            return records
        ei = self._via.get((sender, receiver))
        if ei is not None and self.edges[ei]["id"] in self.lying:
            records = [_altered(records[0]), *records[1:]]
        a, b = self.by_node[sender]["sec"], self.by_node[receiver]["sec"]
        if a != b and len(self.msgs) < MAX_PARTICLES:
            self.msgs.append([a, b])
        return records

    # ---- publishing and alerts -----------------------------------------------------------

    def _level(self, r: dict[str, Any]) -> int:
        return self.level_of[r["sec"]]

    def _publish(self, r: dict[str, Any], type: str, payload: dict[str, Any]) -> Record:
        rec = r["gossip"].publish(type=type, payload=payload, now_ms=self.t * TICK_MS)
        self.total_published += 1
        return rec

    def _inject(self, sector: str) -> None:
        s = self.idx[sector]
        r = next((r for r in self.residents if r["sec"] == s), self.residents[0])
        rec = self._publish(r, "help_request", {
            "kind": "air", "level": self._level(r), "sector": self.sectors[r["sec"]]["id"],
            "urgency": 3, "people": 12, "text": f"Urgent: air failing in {sector}",
        })
        self.probe = (rec["author"], rec["seq"])

    def _on_event(self, resident: int, alert: Alert) -> None:
        if alert["kind"] not in SHOWN_ALERTS:
            self.gap_alerts += 1
            return
        ev = alert["evidence"]
        key = ev.get("claimed_id") or f"{alert['author']}:{alert['seq']}:{alert['kind']}"
        if key in self.alerted:  # one alert per record, not one per node that saw it
            return
        self.alerted[key] = None
        self.events.append({
            "t": "alert", "kind": alert["kind"], "author": alert["author"],
            "peer": alert["peer"], "seq": alert["seq"],
            "evidence": {k: ev[k] for k in ("problem", "claimed_id", "computed_id") if k in ev},
            "tick": self.t, "detected_by": resident,
        })

    # ---- dashboard -----------------------------------------------------------------------

    def holds(self, r: dict[str, Any], author: str, seq: int) -> bool:
        """Does resident r's node hold record (author, seq)? (Also the beacon rule.)"""
        return r["node"].version_vector().get(author, 0) >= seq

    def snapshot(self) -> dict[str, Any]:
        n, tot = len(self.residents), self.total_published
        have = 0
        reached: dict[int, None] = {}
        for r in self.residents:
            vv = r["node"].version_vector()
            have += sum(vv.values())
            if self.probe and vv.get(self.probe[0], 0) >= self.probe[1]:
                reached[self._level(r)] = None
        st = [2 if not e["up"] else
              (1 if self.level_of[self.idx[e["a"]]] in self.dark
               or self.level_of[self.idx[e["b"]]] in self.dark else 0)
              for e in self.edges]
        return {"tick": self.t,
                "residents": [[r["sec"], round(self.sectors[r["sec"]]["x"] + r["jx"], 3),
                               round(self.sectors[r["sec"]]["y"] + r["jy"], 3)]
                              for r in self.residents],
                "edges": st, "dark": self.dark, "net_up": not self.net_cut,
                "messages": self.msgs,
                "syncPct": round(have / (tot * n), 3) if tot else 1.0,
                "probeLevels": sorted(reached), "alerts": self.events, "lying": self.lying}
