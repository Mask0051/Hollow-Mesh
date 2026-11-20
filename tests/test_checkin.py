"""Family check-ins: sealed with a family key, sent from the private feed, readable by
family only (Doc 2 §1.2, prompt #9)."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from hollowmesh.protocol.types import Checkin, CheckinBody
from hollowmesh.services.checkin import family_view, open_checkin, seal
from hollowmesh.sim.runner import Sim
from hollowmesh.sim.world import build_world

FAM, OTHER = b"\x01" * 32, b"\x02" * 32
NONCE = b"\x07" * 24
BODY = {"status": "ok", "level": 2, "sector": "L2-S03", "note": "safe"}


def test_seal_and_open_round_trip():
    payload = seal(FAM, BODY, NONCE)
    Checkin.model_validate(payload)  # a valid wire payload
    assert "safe" not in str(payload) and "L2-S03" not in str(payload)  # nothing in clear
    assert open_checkin([OTHER, FAM], payload) == (1, BODY)


def test_other_families_cannot_read_it():
    assert open_checkin([OTHER], seal(FAM, BODY, NONCE)) is None
    assert open_checkin([], seal(FAM, BODY, NONCE)) is None


def test_tampered_or_junk_ciphertext_opens_to_nothing():
    p = seal(FAM, BODY, NONCE)
    ct = bytearray(base64.b64decode(p["ct"]))
    ct[0] ^= 1
    assert open_checkin([FAM], {**p, "ct": base64.b64encode(ct).decode()}) is None
    for junk in ({}, {"nonce": "!!", "ct": "??"}, {"nonce": 3, "ct": None}):
        assert open_checkin([FAM], junk) is None


def test_bad_bodies_are_refused_before_sealing():
    for bad in ({**BODY, "status": "dead"}, {**BODY, "sector": "L1-S03"}, {**BODY, "note": "x" * 141},
                {**BODY, "extra": 1}):
        with pytest.raises(ValidationError):
            seal(FAM, bad, NONCE)
    CheckinBody.model_validate({k: v for k, v in BODY.items() if k != "note"})  # note optional


def test_checkin_payload_schema():
    good = seal(FAM, BODY, NONCE)
    for bad in ({**good, "nonce": "short"}, {**good, "ct": "not base64!"}, {**good, "level": 1},
                {"ct": good["ct"]}):
        with pytest.raises(ValidationError):
            Checkin.model_validate(bad)


def test_family_view_keeps_newest_first_and_counts_the_rest():
    recs = [{"type": "checkin", "author": "a", "seq": s, "ts": s,
             "payload": seal(k, {**BODY, "note": f"n{s}"}, bytes([s]) * 24)}
            for s, k in ((2, FAM), (3, OTHER), (4, FAM))]
    recs.append({"type": "post", "author": "b", "seq": 2, "ts": 9, "payload": {}})
    mine, sealed = family_view(recs, [FAM])
    assert [b["note"] for _, b in mine] == ["n4", "n2"] and sealed == 1


# --- in the sim ----------------------------------------------------------------------------

N = 80


@pytest.fixture(scope="module")
def world():
    return build_world()


def test_everyone_is_in_one_family_of_two_or_more(world):
    sim = Sim(world, seed=4, n_residents=N)
    members = sorted(m for f in sim.families for m in f["members"])
    assert members == list(range(N))
    assert all(len(f["members"]) >= 2 for f in sim.families)
    assert all(sim.residents[m]["family"] == fid
               for fid, f in enumerate(sim.families) for m in f["members"])


def test_phone_checkin_reaches_family_but_only_family_can_read_it(world):
    sim = Sim(world, seed=4, n_residents=N, sickness=False)
    fam = sim.families[0]["members"]
    me = sim.residents[fam[0]]
    sim.queue_cmd({"cmd": "checkin", "resident": me["id"], "status": "need_help",
                   "note": "lights out"})
    for _ in range(40):
        sim.tick()
    rec = next(x for x in sim.residents[fam[1]]["node"].all_records()
               if x["type"] == "checkin" and x["author"] == me["private"]
               and open_checkin([sim.families[0]["key"]], x["payload"])[1]["note"] == "lights out")
    assert rec["author"] != me["node"].node_id  # private feed: not linked to the public one
    relative = sim.phone(fam[1])
    mine = next(f for f in relative["family"] if f["id"] == me["id"])
    assert mine["status"] in ("need_help", "ok", "hurt", "moving")
    assert any(e["p"].get("note") == "lights out" for e in relative["tabs"]["checkin"])
    stranger = next(r["id"] for r in sim.residents if r["family"] != me["family"])
    assert all(e["p"].get("note") != "lights out" for e in sim.phone(stranger)["tabs"]["checkin"])
    assert sim.phone(stranger)["sealed"] > 0  # it carries check-ins it can't read


def test_bad_checkin_commands_are_ignored(world):
    sim = Sim(world, seed=4, n_residents=N, sickness=False)
    for bad in ({"status": "dead"}, {"status": "ok", "note": "x" * 141}, {"status": "ok", "note": 5},
                {"status": None}, {"note": "hi"}):
        sim._apply({"cmd": "checkin", "resident": 0, **bad})
    for who in (True, N, -1, "0", None):
        sim._apply({"cmd": "checkin", "resident": who, "status": "ok"})
    assert sim._to_send == []
    sim._apply({"cmd": "checkin", "resident": 0, "status": "ok"})  # note is optional
    assert sim._to_send == [(0, "checkin", {"status": "ok", "note": ""})]


def own_checkins(sim: Sim) -> int:
    return sum(1 for r in sim.residents for x in r["node"].all_records()
               if x["author"] == r["private"] and x["type"] == "checkin")


def test_blackout_sets_off_a_wave_of_checkins(world):
    counts = []
    for dark in (False, True):
        sim = Sim(world, seed=6, n_residents=N, sickness=False)
        if dark:
            sim.queue_cmd({"cmd": "blackout", "level": 1})
        for _ in range(30):
            sim.tick()
        counts.append(own_checkins(sim))
    assert counts[1] > counts[0] * 2
