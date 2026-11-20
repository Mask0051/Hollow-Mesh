"""H6: live view. Streams one snapshot per tick over a WebSocket and takes control-panel
commands back (H7). Run with `make demo` (or `python -m hollowmesh.sim --script`) and
open http://localhost:8000.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
from contextlib import asynccontextmanager

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
MAX_SPEED = 10.0


class Clock:
    """Playback speed. Only changes how often we tick, never what a tick does, so the
    same seed gives the same run at any speed.

    {"cmd": "speed", "x": 0.5}  -> half speed;  x = 0 pauses
    {"cmd": "step"}             -> one tick (while paused)
    """

    def __init__(self, speed: float = 1.0) -> None:
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


def create_app(
    seed: int = SEED, script: bool = False, residents: int = 300, speed: float = 1.0
) -> FastAPI:
    world = build_world()
    sim = Sim(world, seed=seed, n_residents=residents, script=DEMO_SCRIPT if script else None)
    clock = Clock(speed)
    clients: list[WebSocket] = []

    async def broadcast(snap: dict) -> None:
        msg = json.dumps({**snap, "speed": clock.speed})
        for ws in list(clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001 — any send failure: drop that client
                clients.remove(ws)

    async def loop() -> None:
        while True:
            if clock.speed > 0:
                await broadcast(sim.tick())
                await asyncio.sleep(TICK_S / clock.speed)
            elif clock.steps:
                clock.steps -= 1
                await broadcast(sim.tick())
            else:
                await asyncio.sleep(0.05)  # paused: just wait for a command

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.sim = sim

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
    async def resident(i: int) -> JSONResponse:
        if not 0 <= i < len(sim.residents):
            return JSONResponse({"error": "no such resident"}, status_code=404)
        return JSONResponse(sim.phone(i))

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        clients.append(ws)
        try:
            while True:  # H7 buttons
                try:
                    cmd = json.loads(await ws.receive_text())
                except json.JSONDecodeError:
                    continue  # ignore junk; the sim validates commands too
                if not clock.handle(cmd):  # speed/step go to the clock, the rest to the sim
                    sim.queue_cmd(cmd)
                elif cmd.get("cmd") == "speed":
                    # Show the new speed now, even if paused. Not a tick: no alerts/particles.
                    await broadcast({**sim.snapshot(), "alerts": [], "messages": []})
        except WebSocketDisconnect:
            if ws in clients:
                clients.remove(ws)

    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m hollowmesh.sim", description=__doc__)
    p.add_argument("--script", action="store_true", help="play the scripted demo attacks")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--residents", type=int, default=300)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--speed", type=float, default=1.0,
                   help=f"start speed: 1 = one tick per {TICK_S}s, 0 = start paused")
    a = p.parse_args(argv)
    print(f"Rat King sim on http://localhost:{a.port}  "
          f"(seed {a.seed}, script {'on' if a.script else 'off'}, speed {a.speed}x)")
    uvicorn.run(create_app(a.seed, a.script, a.residents, a.speed), host=a.host, port=a.port,
                log_level="warning")
