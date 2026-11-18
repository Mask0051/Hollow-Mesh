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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .runner import DEMO_SCRIPT, Sim
from .world import build_world

STATIC = pathlib.Path(__file__).parent / "static"
SEED = 42  # H10: the locked demo seed
TICK_S = 0.1  # wall-clock seconds between ticks


def create_app(seed: int = SEED, script: bool = False, residents: int = 300) -> FastAPI:
    world = build_world()
    sim = Sim(world, seed=seed, n_residents=residents, script=DEMO_SCRIPT if script else None)
    clients: list[WebSocket] = []

    async def loop() -> None:
        while True:
            msg = json.dumps(sim.tick())
            for ws in list(clients):
                try:
                    await ws.send_text(msg)
                except Exception:  # noqa: BLE001 — any send failure: drop that client
                    clients.remove(ws)
            await asyncio.sleep(TICK_S)

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

    @app.get("/world.json")
    def world_json() -> JSONResponse:
        return JSONResponse(world)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        clients.append(ws)
        try:
            while True:  # H7 buttons
                try:
                    sim.queue_cmd(json.loads(await ws.receive_text()))
                except json.JSONDecodeError:
                    pass  # ignore junk; the sim validates commands too
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
    a = p.parse_args(argv)
    print(f"Hollow sim on http://localhost:{a.port}  (seed {a.seed}, script {'on' if a.script else 'off'})")
    uvicorn.run(create_app(a.seed, a.script, a.residents), host=a.host, port=a.port,
                log_level="warning")
