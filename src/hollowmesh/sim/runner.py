"""H4, H5, H10 + P6: the simulated Hollow, running on the real protocol.

Every resident carries a real Node (signed feeds, verified ingest) and a Gossiper that
syncs over a MemoryTransport. The sim decides only WHO CAN REACH WHOM each tick; the
protocol does everything else (Doc 2 §1.6).

Tick loop (1 tick = 10 sim-minutes):
    1. apply queued / scripted commands (Directorate attacks, injects)
    2. residents walk (only through intact edges); sickness spreads (H9)
    3. reachability: who can talk to whom this tick
    4. publish: injected reports + random posts (pushed to up to 3 reachable peers)
    5. every node gossips with up to 2 reachable peers, in a seeded shuffled order
    6. snapshot for the dashboard

Reachability rules (Doc 2 §1.6):
    * Mesh: same sector, each node links to <=3 sampled neighbours. Each intact tunnel
      or stairwell links one random pair across it.
    * Stairwells are the only links between levels. A cut stairwell carries no links
      and no movement.
    * Two switches for the demo: the Directorate network ("Wi-Fi") and Rat King, our mesh.
      Mesh off = no phone-to-phone or relay links, so only Wi-Fi can carry anything.
    * Directorate network: while it's up, every node gets 1 random long-range link per
      tick. Cutting it leaves only the mesh plus people walking.
    * Blackout: nodes on that level take part with probability 0.3 per tick, mesh only:
      the Directorate network needs mains power, so a dark level gets no Wi-Fi links.
    * Lying relay (P7): records crossing a lying edge get their payload rewritten in
      transit. The signature no longer matches, so the receiving node raises a tamper alert.

Countertally (P8): sick residents file signed health_reports. stats() counts them from
ONE observer phone's store (whatever that phone has received), next to the Directorate's
official undercount and the hidden ground truth.

Families (prompt #9): residents are grouped into families of 2-4, scattered across
levels. Each family shares a secret key. A check-in is sealed with it and published from
the member's PRIVATE feed: every phone carries it, only family can read it. Phones check
in now and then, and much more often when their level loses power.

Water (prompt #4): each level drinks from one recycler. When one breaks, residents on
that level notice (smell, colour, taste, or getting sick) and file a water_report, once
each per failure; a few false alarms happen everywhere. Bad water also makes people sick.

Phones (demo): click a resident to post or send a beacon colour from their node. Each
resident is drawn in the colour of the newest beacon their own node holds.

Deterministic: one seeded random.Random for everything (keys, walking, links, gossip),
and no set is ever iterated. Same seed + same script = same run.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from ..protocol.node import Alert, Node
from ..protocol.record import Keypair, Record, RecordProblem, check_record
from ..protocol.sync import Gossiper
from ..protocol.transport.memory import MemoryTransport
from ..protocol.types import PAYLOAD_SCHEMAS, CheckinBody
from ..services.checkin import family_view, seal
from ..services.countertally import tally
from ..services.water import water_status
from .sickness import Sickness

TICK_MS = 600_000  # 1 tick = 10 sim-minutes; record ts is sim time, never wall time
WALK_P, POST_P = 0.08, 0.001
SECTOR_LINKS = 3  # mesh: each node links to <=3 others in its sector
BLACKOUT_ON_P = 0.3  # a node on a dark level is up this tick with this probability
MAX_PARTICLES = 60  # cross-sector message particles per snapshot
SHOWN_ALERTS = ("tamper", "fork")  # gaps are routine during gossip; counted, not shown
DEFAULT_COLOR = "#ffe27a"  # everyone's colour until a beacon reaches them
FEED_LEN = 40  # newest records shown on a resident's phone (per tab)
PHONE_TABS = ("help_request", "health_report", "water_report", "checkin")  # posts+colours: All
WATER_NOTICE_P, WATER_FALSE_P = 0.006, 0.00003  # per resident per tick: bad water / false alarm
WATER_ISSUES = (("smell", 35), ("color", 30), ("taste", 25))  # what people notice first
CHECKIN_P, CHECKIN_DARK_P = 0.0015, 0.02  # per resident per tick: normally / power out
FAMILY_SIZES = (2, 3, 3, 4)
SOS_SHOWN, SOS_MAX = 144, 6  # map markers: SOS calls from the last sim day, newest 6

# Background chatter, per level: {room} is where the sender is standing.
CHATTER = [
    ["Gate guards doubled at the {room} again", "Anyone know when the next surface probe goes up?",
     "Directorate notice: curfew moved to 21:00", "Lift to L1 is jammed, take the stairs",
     "Saw a drone crew heading to the {room}", "Comms says the Wi-Fi outage is 'maintenance'",
     "Air in the {room} smells like ozone", "Who left the blast door log open?"],
    ["Mess Hall has soup tonight, bring a cup", "Lost: a grey cat near the {room}",
     "Kids' lessons moved to the Rec Room", "Clinic queue is out the door, go early",
     "Trading two batteries for a filter, {room}", "Water pressure is low on Homes again",
     "Anyone have a spare blanket? Dorm B is freezing", "Laundry is running, 30 min wait"],
    ["Tomato harvest in Garden B this week", "Pump Hall is making that noise again",
     "Need two hands at the {room} after shift", "Seed Bank inventory: we're short on beans",
     "Recycler output looks cloudy, don't drink yet", "Forge has spare pipe fittings",
     "Grow lights flickered for an hour in the {room}", "Hydroponics needs nitrate, who has some?"],
    ["Heat in the {room} is brutal today", "Coolant line hissing again, stay clear",
     "Mine Face shift cut short, bad air", "Vent Hub fans are down to half speed",
     "Anyone heading up? Carry this to the Clinic", "Generators running rough since the blackout",
     "My chest hurts after shift, anyone else?", "Masks at the {room} are all used up"],
]
CHECKIN_NOTES = {
    "ok": ["All fine here", "Safe, staying put", "We're ok, power's holding", "Fine, at work", ""],
    "hurt": ["Bad cough, staying in bed", "Fever since yesterday", "Chest is tight, resting"],
    "need_help": ["Lights are out, we're scared", "No power, need batteries", "Can't reach the stairs"],
    "moving": ["Heading up to see you", "Moving to the Mess Hall", "Going down for shift"],
}
_HEX = frozenset("0123456789abcdef")

DEMO_SCRIPT: dict[int, list[dict[str, Any]]] = {  # tick -> commands (H10)
    10: [{"cmd": "cut_network"}],
    12: [{"cmd": "cut_stairwell", "id": "ST-2-3-a"}],  # the Deep's only shaft
    15: [{"cmd": "inject", "sector": "L3-S04"}],  # SOS: stuck on level 3...
    60: [{"cmd": "toggle_stairwell", "id": "ST-2-3-a"}],  # ...until the shaft reopens
    100: [{"cmd": "blackout", "level": 2}],
    140: [{"cmd": "heal"}],
    160: [{"cmd": "tamper", "id": "ST-0-1-b"}, {"cmd": "inject", "sector": "L1-S05"}],
    220: [{"cmd": "heal"}],
    230: [{"cmd": "water", "level": 3, "ok": False}],  # the Deep's recycler fails
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


def _text(c: dict[str, Any], *keys: str) -> str | None:
    """The first of `keys` holding a string. Commands come from the browser, so an id
    could be a list or a dict, which would crash a dict lookup."""
    for k in keys:
        v = c.get(k)
        if isinstance(v, str):
            return v
    return None


class Sim:
    """The simulated Hollow. Also the `Network` that every MemoryTransport asks."""

    def __init__(
        self,
        world: dict[str, Any],
        seed: int = 42,
        n_residents: int = 300,
        script: dict[int, list[dict[str, Any]]] | None = None,
        sickness: bool = True,
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
        self.net_cut = False  # Directorate network ("Wi-Fi") down
        self.mesh_off = False  # Rat King (the mesh) switched off: no mesh links at all
        self.dark: list[int] = []
        self.lying: list[str] = []  # edge ids with a lying relay on them
        self.recyclers: list[dict[str, Any]] = world.get("recyclers", [])
        self.water_bad: dict[int, int] = {}  # level -> tick its recycler broke
        self.phones_off: list[bool] = [False] * n_residents  # dead phones (metrics: P9)
        self.altered_sent = 0  # tampered copies the lying relays forwarded

        self.total_published = 0
        self.probe: tuple[str, int] | None = None  # (author, seq) of the latest inject
        self.alerted: dict[str, None] = {}  # records already alerted on (ordered set)
        self.gap_alerts = 0
        self.events: list[dict[str, Any]] = []
        self.msgs: list[list[int]] = []
        self._to_inject: list[str] = []
        # Phone sends (resident, type, payload fields), published after reachability.
        self._to_send: list[tuple[int, str, dict[str, Any]]] = []
        # Every beacon ever published, oldest first: (author, seq, color). A resident's
        # colour is the newest one their node holds.
        self.beacons: list[tuple[str, int, str]] = []
        # Every SOS (help_request) ever sent, oldest first, for the map markers.
        self.sos: list[dict[str, Any]] = []

        self._links: dict[str, list[str]] = {}  # node_id -> reachable node_ids this tick
        self._via: dict[tuple[str, str], int] = {}  # (a, b) -> edge index the link runs over

        verify = VerifyCache()
        self.residents: list[dict[str, Any]] = []
        self.by_node: dict[str, dict[str, Any]] = {}
        for i in range(n_residents):
            keys = Keypair.from_seed(self.rng.randbytes(32))  # seeded, so ids repeat per seed
            private = Keypair.from_seed(self.rng.randbytes(32))  # check-in feed, unlinkable
            node = Node(keys, private_keys=private, check=verify,
                        on_event=lambda a, i=i: self._on_event(i, a))
            r = {"id": i, "sec": self.rng.randrange(len(self.sectors)), "node": node,
                 "private": private.public,
                 "jx": self.rng.uniform(-.38, .38), "jy": self.rng.uniform(-.05, .05)}
            r["gossip"] = Gossiper(node, MemoryTransport(node.node_id, self, self._relay), self.rng)
            self.residents.append(r)
            self.by_node[node.node_id] = r

        self.by_author = {**{r["node"].node_id: r for r in self.residents},
                          **{r["private"]: r for r in self.residents}}
        for r in self.residents:  # every feed starts with a profile (seq 1)
            self._publish(r, "profile", {"kind": "public", "level": self._level(r)})
            # The private feed starts at the first check-in (fewer feeds to sync till then).

        # Families: shuffled, so relatives live on different levels. Keys are local only.
        order = list(range(n_residents))
        self.rng.shuffle(order)
        self.families: list[dict[str, Any]] = []
        while order:
            size = self.rng.choice(FAMILY_SIZES)
            members, order = order[:size], order[size:]
            if len(members) < 2 and self.families:  # a lone leftover joins the last family
                self.families[-1]["members"] += members
            else:
                self.families.append({"members": members, "key": self.rng.randbytes(32)})
        for fid, f in enumerate(self.families):
            f["members"].sort()
            for m in f["members"]:
                self.residents[m]["family"] = fid

        self.sickness = Sickness() if sickness else None
        # Countertally runs on this resident's phone: someone who starts in the Clinic.
        clinic = next((i for i, s in enumerate(self.sectors) if s["name"] == "Clinic"), -1)
        self.observer = next((r["id"] for r in self.residents if r["sec"] == clinic), 0)

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
        elif k in ("network", "mesh"):  # dashboard switches: {"cmd": "mesh", "on": false}
            on = c.get("on")
            if isinstance(on, bool):
                if k == "network":
                    self.net_cut = not on
                else:
                    self.mesh_off = not on
        elif k in ("blackout", "power"):  # dashboard: {"cmd": "power", "level": 2, "on": true}
            lv, on = c.get("level"), c.get("on", k == "power")
            if (isinstance(lv, int) and not isinstance(lv, bool) and 0 <= lv < self.levels
                    and isinstance(on, bool)):
                if not on and lv not in self.dark:
                    self.dark.append(lv)
                elif on and lv in self.dark:
                    self.dark.remove(lv)
        elif k in ("cut_stairwell", "cut_stair"):
            ei = self.eidx.get(_text(c, "id", "edge"))
            if ei is None:  # default: first stairwell still up
                ei = next((i for i, e in enumerate(self.edges)
                           if e["kind"] == "stairwell" and e["up"]), None)
            if ei is not None:
                self.edges[ei]["up"] = False
        elif k == "toggle_stairwell":  # dashboard click: cut <-> reopen one stairwell
            ei = self.eidx.get(_text(c, "id"))
            if ei is not None and self.edges[ei]["kind"] == "stairwell":
                self.edges[ei]["up"] = not self.edges[ei]["up"]
        elif k in ("tamper", "deploy_relay"):
            eid = _text(c, "id", "edge")
            if eid not in self.eidx:  # default: first stairwell
                eid = next(e["id"] for e in self.edges if e["kind"] == "stairwell")
            if eid not in self.lying:
                self.lying.append(eid)
        elif k == "relay":  # dashboard switch: {"cmd": "relay", "on": false} removes all liars
            on = c.get("on")
            if on is True:
                self._apply({"cmd": "tamper"})
            elif on is False:
                self.lying = []
        elif k == "inject":
            sector = _text(c, "sector") or "L3-S04"
            if sector in self.idx:
                self._to_inject.append(sector)  # published after this tick's reachability
        elif k in ("post", "beacon", "help", "health", "checkin"):  # from a resident's phone
            i = c.get("resident")
            if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(self.residents):
                return
            if k == "checkin":
                r = self.residents[i]
                body = {"status": c.get("status"), "note": c.get("note", ""),
                        "level": self._level(r), "sector": self.sectors[r["sec"]]["id"]}
                try:
                    CheckinBody.model_validate(body)
                except ValidationError:
                    return
                self._to_send.append((i, "checkin", {"status": body["status"], "note": body["note"]}))
            elif k in ("help", "health"):
                self._queue_report(i, k, c)
            elif k == "post":
                text = c.get("text")
                if isinstance(text, str) and 0 < len(text.strip()) and len(text) <= 280:
                    self._to_send.append((i, "post", {"text": text}))
            else:
                color, label = c.get("color"), c.get("label", "")
                if (isinstance(color, str) and len(color) == 7 and color[0] == "#"
                        and set(color[1:].lower()) <= _HEX
                        and isinstance(label, str) and len(label) <= 40):
                    self._to_send.append((i, "beacon", {"color": color.lower(), "label": label}))
        elif k == "water":  # {"cmd": "water", "level": 3, "ok": false} breaks that recycler
            lv, ok = c.get("level"), c.get("ok")
            if (isinstance(lv, int) and not isinstance(lv, bool) and 0 <= lv < self.levels
                    and isinstance(ok, bool)):
                if ok:
                    self.water_bad.pop(lv, None)
                elif lv not in self.water_bad:
                    self.water_bad[lv] = self.t
        elif k == "water_report":  # from a phone: {"resident", "issue", "severity"}
            i = c.get("resident")
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(self.residents):
                self._queue_water(self.residents[i], c.get("issue"), c.get("severity"))
        elif k == "observe":  # dashboard: run Countertally on this resident's phone
            i = c.get("resident")
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(self.residents):
                self.observer = i
        elif k == "heal":
            self.net_cut, self.mesh_off, self.dark, self.lying = False, False, [], []
            self.water_bad = {}
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
        if self.sickness:
            self.sickness.step(self)  # queues the health reports that are due
        for r in self.residents:  # water: people on a bad level notice; rare false alarms
            lv = self._level(r)
            broke = self.water_bad.get(lv)
            if broke is not None and r.get("water_seen") != (lv, broke):
                if self.rng.random() < WATER_NOTICE_P:
                    r["water_seen"] = (lv, broke)  # one report per person per failure
                    self._queue_water(r, self._water_issue(r), self.rng.choice((2, 2, 3)))
            elif self.rng.random() < WATER_FALSE_P:
                self._queue_water(r, self._water_issue(r), 1)
        for r in self.residents:  # family check-ins: far more often when the lights go out
            dark = self._level(r) in self.dark
            if self.rng.random() < (CHECKIN_DARK_P if dark else CHECKIN_P):
                self._to_send.append((r["id"], "checkin", self._auto_checkin(r, dark)))

        self._reachability()

        for sector in self._to_inject:
            self._inject(sector)
        self._to_inject = []
        for i, type, fields in self._to_send:
            r = self.residents[i]
            if type != "beacon" and "level" not in fields:  # where they are when it's sent
                fields = {**fields, "level": self._level(r), "sector": self.sectors[r["sec"]]["id"]}
            if type == "checkin":  # sealed for the family, from the private feed
                key = self.families[r["family"]]["key"]
                if not r.get("private_feed"):  # first check-in: open the private feed
                    r["private_feed"] = True
                    self._publish(r, "profile", {"kind": "private", "level": self._level(r)},
                                  identity="private")
                self._publish(r, "checkin", seal(key, fields, self.rng.randbytes(24)),
                              identity="private")
            else:
                self._publish(r, type, fields)
        self._to_send = []
        for r in self.residents:
            if self.rng.random() < POST_P:
                room = self.sectors[r["sec"]]
                text = self.rng.choice(CHATTER[room["level"]]).format(room=room["name"])
                self._publish(r, "post", {"text": text, "level": room["level"],
                                          "sector": room["id"]})

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
        on = [r for r in self.residents if not self.phones_off[r["id"]]
              and (self.level_of[r["sec"]] not in self.dark or self.rng.random() < BLACKOUT_ON_P)]
        by: dict[int, list[str]] = defaultdict(list)
        for r in on:
            by[r["sec"]].append(r["node"].node_id)

        if not self.mesh_off:
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
            # Its access points need mains power: a dark level gets no Wi-Fi at all.
            ids = [r["node"].node_id for r in on if self.level_of[r["sec"]] not in self.dark]
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
            self.altered_sent += 1
        a, b = self.by_node[sender]["sec"], self.by_node[receiver]["sec"]
        if a != b and len(self.msgs) < MAX_PARTICLES:
            self.msgs.append([a, b])
        return records

    # ---- publishing and alerts -----------------------------------------------------------

    def _level(self, r: dict[str, Any]) -> int:
        return self.level_of[r["sec"]]

    def _queue_report(self, i: int, k: str, c: dict[str, Any]) -> None:
        """Phone SOS ({"cmd": "help", kind, urgency, people, text}) or health report
        ({"cmd": "health", sym, severity, sick, ago}). Checked against the real schema
        now, so junk is dropped here instead of failing at publish."""
        if k == "help":
            type = "help_request"
            fields = {f: c.get(f) for f in ("kind", "urgency", "people", "text")}
        else:
            type = "health_report"
            ago = c.get("ago", 0)
            if not isinstance(ago, int) or isinstance(ago, bool) or not 0 <= ago <= 14:
                return
            fields = {f: c.get(f) for f in ("sym", "severity", "sick")}
            fields["onset_day"] = max(0, self.day - ago)
        r = self.residents[i]
        try:  # with the sender's current location (refreshed again when it's sent)
            PAYLOAD_SCHEMAS[type].model_validate(
                {**fields, "level": self._level(r), "sector": self.sectors[r["sec"]]["id"]})
        except ValidationError:
            return
        self._to_send.append((i, type, fields))

    def _water_issue(self, r: dict[str, Any]) -> str:
        case = self.sickness.had.get(r["id"]) if self.sickness else None
        if case is not None and case.ends > self.t and case.cause == "water":
            return "illness"
        names, weights = zip(*WATER_ISSUES, strict=True)
        return self.rng.choices(names, weights)[0]

    def _queue_water(self, r: dict[str, Any], issue: Any, severity: Any) -> None:
        """A water_report about the recycler on r's level, from where r is now."""
        lv = self._level(r)
        fields = {"recycler": f"R{lv}", "level": lv, "sector": self.sectors[r["sec"]]["id"],
                  "issue": issue, "severity": severity}
        try:
            PAYLOAD_SCHEMAS["water_report"].model_validate(fields)
        except ValidationError:
            return
        self._to_send.append((r["id"], "water_report", fields))

    def _auto_checkin(self, r: dict[str, Any], dark: bool) -> dict[str, Any]:
        sk = self.sickness
        case = sk.had.get(r["id"]) if sk else None
        if case is not None and case.ends > self.t:
            status = "hurt"
        elif dark:
            status = "need_help" if self.rng.random() < 0.3 else "ok"
        else:
            status = "moving" if self.rng.random() < 0.1 else "ok"
        return {"status": status, "note": self.rng.choice(CHECKIN_NOTES[status])}

    @property
    def day(self) -> int:
        """Sim day, from 0 (a tick is TICK_MS of sim time)."""
        return self.t * TICK_MS // 86_400_000

    def _publish(self, r: dict[str, Any], type: str, payload: dict[str, Any],
                 identity: str = "public") -> Record:
        rec = r["gossip"].publish(type=type, payload=payload, now_ms=self.t * TICK_MS,
                                  identity=identity)
        self.total_published += 1
        if type == "beacon":
            self.beacons.append((rec["author"], rec["seq"], payload["color"]))
        elif type == "help_request":
            self.sos.append({"author": rec["author"], "seq": rec["seq"], "from": r["id"],
                             "sector": self.idx[payload["sector"]], "kind": payload["kind"],
                             "people": payload["people"], "urgency": payload["urgency"],
                             "text": payload["text"], "tick": self.t})
        return rec

    def _inject(self, sector: str) -> None:
        s = self.idx[sector]
        # Someone in that room; if it's empty, someone on that level; else anyone.
        r = (next((r for r in self.residents if r["sec"] == s), None)
             or next((r for r in self.residents if self._level(r) == self.level_of[s]), None)
             or self.residents[0])
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

    def color_of(self, vv: dict[str, int]) -> str:
        """The newest beacon this node holds, or the default. Holding = vv[author] >= seq."""
        for author, seq, color in reversed(self.beacons):
            if vv.get(author, 0) >= seq:
                return color
        return DEFAULT_COLOR

    def name(self, author: str) -> str:
        r = self.by_author.get(author)
        return f"r{r['id']}" if r else author[:8]

    def phone(self, i: int) -> dict[str, Any]:
        """Resident i's phone: what THEIR node holds right now, newest first."""
        r = self.residents[i]
        node: Node = r["node"]
        vv = node.version_vector()
        everything = node.all_records()
        fam = self.families[r["family"]]
        opened, sealed = family_view(everything, [fam["key"]])  # only family can read these
        body = {id(x): b for x, b in opened}
        recs = [x for x in everything if x["type"] not in ("profile", "checkin")]
        recs += [x for x, _ in opened]
        recs.sort(key=lambda x: (x["ts"], x["author"], x["seq"]), reverse=True)

        def entry(x: Record) -> dict[str, Any]:
            p = body.get(id(x), x["payload"])  # a check-in shows its decrypted body
            return {"from": self.name(x["author"]),
                    "mine": x["author"] in (node.node_id, r["private"]),
                    "type": x["type"], "seq": x["seq"], "tick": x["ts"] // TICK_MS,
                    "text": p.get("text"), "color": p.get("color"),
                    "label": p.get("label"), "sector": p.get("sector"), "p": dict(p)}

        feed = [entry(x) for x in recs[:FEED_LEN]]
        tabs: dict[str, list[dict[str, Any]]] = {t: [] for t in PHONE_TABS}
        counts = dict.fromkeys(PHONE_TABS, 0)
        for x in recs:  # newest first, so each tab keeps its newest FEED_LEN
            if x["type"] in tabs:
                counts[x["type"]] += 1
                if len(tabs[x["type"]]) < FEED_LEN:
                    tabs[x["type"]].append(entry(x))
        family = []
        for m in fam["members"]:  # latest check-in per member replaces older ones
            mr = self.residents[m]
            last = next(((x, b) for x, b in opened if x["author"] == mr["private"]), None)
            e = {"id": m, "name": f"r{m}", "me": m == i}
            if last:
                x, b = last
                e |= {"status": b["status"], "note": b["note"], "level": b["level"],
                      "room": self.sectors[self.idx[b["sector"]]]["name"],
                      "tick": x["ts"] // TICK_MS}
            family.append(e)
        sector = self.sectors[r["sec"]]
        return {"id": i, "name": f"r{i}", "node_id": node.node_id, "sector": sector["id"],
                "room": sector["name"], "level": sector["level"],
                "peers": len(self._links.get(node.node_id, [])), "color": self.color_of(vv),
                "held": sum(vv.values()), "total": self.total_published, "feed": feed,
                "tabs": tabs, "counts": counts, "day": self.day,
                "family": family, "sealed": sealed,
                "water": water_status(everything, self.t * TICK_MS, self.recyclers, TICK_MS)}

    def stats(self) -> dict[str, Any]:
        """Countertally's split screen (Doc 2 §1.6 "stats"), all lists indexed by level.

        countertally/staleness/flags come only from the observer phone's own store.
        official is the Directorate's undercount; truth is for judges and eval only.
        """
        r = self.residents[self.observer]
        recs = r["node"].all_records()
        out = tally(recs, self.t * TICK_MS, self.levels, len(self.sectors), TICK_MS)
        out["water"] = water_status(recs, self.t * TICK_MS, self.recyclers, TICK_MS)
        sk, day = self.sickness, self.day
        out["official"] = sk.official(self.t, day, self.levels) if sk else [0] * self.levels
        out["truth"] = sk.truth(self.t, day, self.levels) if sk else [0] * self.levels
        for f in out["flags"]:
            f["name"] = self.sectors[self.idx[f["sector"]]]["name"] if f["sector"] in self.idx else ""
        sec = self.sectors[r["sec"]]
        out["observer"] = {"id": self.observer, "name": f"r{self.observer}",
                           "room": sec["name"], "level": sec["level"]}
        return out

    def _sos_markers(self) -> list[dict[str, Any]]:
        """Recent SOS calls, newest first, each with the levels whose phones now hold it."""
        out = []
        for s in reversed(self.sos):
            if self.t - s["tick"] > SOS_SHOWN or len(out) == SOS_MAX:
                break
            reached = sorted({self._level(r) for r in self.residents
                              if self.holds(r, s["author"], s["seq"])})
            out.append({k: s[k] for k in ("from", "sector", "kind", "people", "urgency", "text", "tick")}
                       | {"levels": reached})
        return out

    def snapshot(self) -> dict[str, Any]:
        n, tot = len(self.residents), self.total_published
        have = 0
        reached: dict[int, None] = {}
        colors: list[str] = []
        for r in self.residents:
            vv = r["node"].version_vector()
            have += sum(vv.values())
            colors.append(self.color_of(vv))
            if self.probe and vv.get(self.probe[0], 0) >= self.probe[1]:
                reached[self._level(r)] = None
        st = [2 if not e["up"] else
              (1 if self.level_of[self.idx[e["a"]]] in self.dark
               or self.level_of[self.idx[e["b"]]] in self.dark else 0)
              for e in self.edges]
        sec = self.sectors
        return {"tick": self.t,
                "residents": [[r["sec"], round(sec[r["sec"]]["x"] + r["jx"] * sec[r["sec"]]["w"], 3),
                               round(sec[r["sec"]]["y"] + r["jy"], 3)]  # jx: share of width
                              for r in self.residents],
                "colors": colors,
                "edges": st, "dark": self.dark, "net_up": not self.net_cut,
                "mesh_up": not self.mesh_off,
                "messages": self.msgs,
                "syncPct": round(have / (tot * n), 3) if tot else 1.0,
                "probeLevels": sorted(reached), "alerts": self.events, "lying": self.lying,
                "sick": self.sickness.sick_now(self.t) if self.sickness else [],
                "sos": self._sos_markers(),
                "water_bad": sorted(self.water_bad),
                "stats": self.stats()}
