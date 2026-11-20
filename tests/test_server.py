"""The live server: shared mode (local) and hosted mode (one sim per visitor)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hollowmesh.sim import server


def app(hosted: bool) -> TestClient:
    return TestClient(server.create_app(residents=40, speed=10.0, hosted=hosted))


def hello(ws) -> dict:
    return json.loads(ws.receive_text())


def snap_after(ws, n: int = 3) -> dict:
    for _ in range(n):
        m = json.loads(ws.receive_text())
    return m


def test_shared_mode_everyone_watches_one_sim():
    with app(hosted=False) as c, c.websocket_connect("/ws") as a, c.websocket_connect("/ws") as b:
        assert hello(a)["session"] == hello(b)["session"] == "shared"
        a.send_text(json.dumps({"cmd": "network", "on": False}))
        assert snap_after(b, 5)["net_up"] is False  # b sees a's click


def test_hosted_visitors_get_their_own_sims():
    with app(hosted=True) as c, c.websocket_connect("/ws") as a, c.websocket_connect("/ws") as b:
        ha, hb = hello(a), hello(b)
        assert ha["session"] != hb["session"] and ha["hosted"]
        a.send_text(json.dumps({"cmd": "network", "on": False}))
        assert snap_after(a, 5)["net_up"] is False
        assert snap_after(b, 5)["net_up"] is True  # a's click doesn't touch b's sim
        r = c.get(f"/resident/0?s={ha['session']}")
        assert r.status_code == 200 and r.json()["name"] == "r0"
        assert c.get("/resident/0?s=nope").status_code == 404


def test_hosted_sessions_are_capped_and_freed(monkeypatch):
    monkeypatch.setattr(server, "MAX_SESSIONS", 1)
    with app(hosted=True) as c:
        with c.websocket_connect("/ws") as a:
            hello(a)
            with c.websocket_connect("/ws") as b:
                assert hello(b) == {"t": "full", "max": 1}
        assert c.app.state.sessions == {}  # a left: its sim is gone
        with c.websocket_connect("/ws") as d:  # room again
            assert hello(d)["t"] == "hello"


def test_idle_hosted_session_ends(monkeypatch):
    monkeypatch.setattr(server, "IDLE_S", 0.3)
    with app(hosted=True) as c, c.websocket_connect("/ws") as a:
        hello(a)
        for _ in range(200):
            m = json.loads(a.receive_text())
            if m.get("t") == "idle":
                break
        else:
            pytest.fail("no idle message")


def test_icons_only_known_names():
    with app(hosted=True) as c:
        assert c.get("/icons/rat-king.svg").status_code == 200
        assert c.get("/icons/README.svg").status_code == 404
