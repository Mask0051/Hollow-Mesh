"""H6: live view. Streams one snapshot per tick over a WebSocket and takes control-panel
commands back (H7). Run with `make demo` (or `python -m hollowmesh.sim --script`) and
open http://localhost:8000.

Two modes:
  * shared (default, local): one sim; every open tab watches and drives the same run.
  * --hosted (the public demo): every visitor gets their own sim, so nobody's clicks or
    pauses affect anyone else. At most MAX_SESSIONS at once; a session ends when its tab
    closes or after IDLE_S without a click. (Doc 2 §1.6 risk register.)

A sim only ticks while someone is watching it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .runner import DEMO_SCRIPT, Sim
from .world import build_world

STATIC = pathlib.Path(__file__).parent / "static"
ICONS = pathlib.Path(__file__).resolve().parents[3] / "docs" / "icons"  # the team's designs
ICON_NAMES = ("rat-king", "counter-tally", "tail-count", "taster", "sos")
SEED = 42  # H10: the locked demo seed
TICK_S = 0.1  # wall-clock seconds between ticks at 1x
START_SPEED = 0.5  # 5 ticks/s: easy to follow on screen, and half the CPU of 1x
MAX_SPEED = 10.0
MAX_SESSIONS = 5  # hosted: sims running at once
IDLE_S = 15 * 60  # hosted: end a session after this long without a command


class Clock:
    """Playback speed. Only changes how often we tick, never what a tick does, so the
    same seed gives the same run at any speed.

    {"cmd": "speed", "x": 0.5}  -> half speed;  x = 0 pauses
    {"cmd": "step"}             -> one tick (while paused)
    """

    def __init__(self, speed: float = START_SPEED) -> None:
        self.speed = speed
        self.steps = 0  # ticks requested while paused

    def handle(self, c: object) -> bool:
        """Apply a speed/step command. Returns False if `c` isn't one (it's for the sim)."""
        if not isinstance(c, dict):
            return False
        if c.get("cmd") == "speed":
            x = c.get("x")
            if isinstance(x, int | float) and not isinstance(x, bool) and x >= 0:
                self.speed = min(float(x), MAX_SPEED)
            return True
        if c.get("cmd") == "step":
            self.steps += 1
            return True
        return False


class Session:
    """One sim, its playback clock, and the WebSockets watching it."""

    def __init__(self, sim: Sim, speed: float, idle_s: float | None = None,
                 sid: str | None = None) -> None:
        self.id = sid or secrets.token_urlsafe(9)
        self.idle_s = idle_s  # hosted: close the session after this long without a command
        self.sim = sim
        self.clock = Clock(speed)
        self.clients: list[WebSocket] = []
        self.last_cmd = time.monotonic()

    async def broadcast(self, snap: dict[str, Any]) -> None:
        msg = json.dumps({**snap, "speed": self.clock.speed})
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001 — any send failure: drop that client
                self.clients.remove(ws)

    async def loop(self) -> None:
        while True:
            if self.idle_s is not None and time.monotonic() - self.last_cmd > self.idle_s:
                msg = json.dumps({"t": "idle", "minutes": round(self.idle_s / 60)})
                for ws in list(self.clients):
                    try:
                        await ws.send_text(msg)
                        await ws.close()
                    except Exception:  # noqa: BLE001,S110 — already gone is fine
                        pass
                self.clients.clear()
                return
            if not self.clients:
                await asyncio.sleep(0.2)  # nobody watching: don't tick
            elif self.clock.speed > 0:
                await self.broadcast(self.sim.tick())
                await asyncio.sleep(TICK_S / self.clock.speed)
            elif self.clock.steps:
                self.clock.steps -= 1
                await self.broadcast(self.sim.tick())
            else:
                await asyncio.sleep(0.05)  # paused: just wait for a command

    async def command(self, cmd: Any) -> None:
        self.last_cmd = time.monotonic()
        if not self.clock.handle(cmd):  # speed/step go to the clock, the rest to the sim
            self.sim.queue_cmd(cmd)
        elif cmd.get("cmd") == "speed":
            # Show the new speed now, even if paused. Not a tick: no alerts/particles.
            await self.broadcast({**self.sim.snapshot(), "alerts": [], "messages": []})


def create_app(
    seed: int = SEED, script: bool = False, residents: int = 300,
    speed: float = START_SPEED, hosted: bool = False,
) -> FastAPI:
    world = build_world()

    def new_sim() -> Sim:
        return Sim(world, seed=seed, n_residents=residents, script=DEMO_SCRIPT if script else None)

    shared = None if hosted else Session(new_sim(), speed, sid="shared")
    sessions: dict[str, Session] = {} if shared is None else {"shared": shared}
    tasks: dict[str, asyncio.Task[None]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if shared is not None:
            tasks["shared"] = asyncio.create_task(shared.loop())
        yield
        for t in tasks.values():
            t.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.sim = shared.sim if shared else None  # shared mode only (scripts/tests)
    app.state.sessions = sessions

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/icons/{name}.svg")
    def icon(name: str) -> FileResponse:
        if name not in ICON_NAMES or not (ICONS / f"{name}.svg").is_file():
            raise HTTPException(status_code=404)  # only the known icons, never a path
        return FileResponse(ICONS / f"{name}.svg", media_type="image/svg+xml")

    @app.get("/world.json")
    def world_json() -> JSONResponse:
        return JSONResponse(world)

    # async on purpose: it runs on the same event loop as the tick loop, so it never
    # reads the sim halfway through a tick (a plain def would run in a thread).
    @app.get("/resident/{i}")
    async def resident(i: int, s: str = "shared") -> JSONResponse:
        sess = sessions.get(s)
        if sess is None:
            return JSONResponse({"error": "no such session"}, status_code=404)
        if not 0 <= i < len(sess.sim.residents):
            return JSONResponse({"error": "no such resident"}, status_code=404)
        return JSONResponse(sess.sim.phone(i))

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        if shared is not None:
            sess = shared
        elif len(sessions) >= MAX_SESSIONS:
            await ws.send_text(json.dumps({"t": "full", "max": MAX_SESSIONS}))
            await ws.close()
            return
        else:
            sess = Session(new_sim(), speed, idle_s=IDLE_S)
            sessions[sess.id] = sess
            tasks[sess.id] = asyncio.create_task(sess.loop())
        await ws.send_text(json.dumps({"t": "hello", "session": sess.id, "hosted": hosted}))
        sess.clients.append(ws)
        try:
            while True:  # H7 buttons
                raw = await ws.receive_text()
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # ignore junk; the sim validates commands too
                await sess.command(cmd)
        except (WebSocketDisconnect, RuntimeError):  # closed by them, or by the idle check
            pass
        finally:
            if ws in sess.clients:
                sess.clients.remove(ws)
            if hosted and not sess.clients:  # their own sim: free it
                sessions.pop(sess.id, None)
                task = tasks.pop(sess.id, None)
                if task:
                    task.cancel()

    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m hollowmesh.sim", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--script", action="store_true", help="play the scripted demo attacks")
    p.add_argument("--hosted", action="store_true",
                   help=f"public demo: one sim per visitor (max {MAX_SESSIONS})")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--residents", type=int, default=300)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--speed", type=float, default=START_SPEED,
                   help=f"start speed: 1 = one tick per {TICK_S}s, 0 = start paused")
    a = p.parse_args(argv)
    print(f"Rat King sim on http://localhost:{a.port}  (seed {a.seed}, script "
          f"{'on' if a.script else 'off'}, speed {a.speed}x, "
          f"{'one sim per visitor' if a.hosted else 'one shared sim'})")
    uvicorn.run(create_app(a.seed, a.script, a.residents, a.speed, a.hosted), host=a.host,
                port=a.port, log_level="warning")
