"""The simulated Hollow. Owner: teammate (H2–H10): world map, residents, stairwells,
Directorate attacks, simple sickness, live view. P6 (MK): it runs on the real protocol.

    world.py    H2  floor plan -> spec/world.json
    runner.py   H4, H5, H10 + P6/P7  the tick loop on real Nodes + Gossipers
    server.py   H6  FastAPI + WebSocket live view; static/index.html is the dashboard

Rules: all randomness comes from one random.Random(seed); never use the global `random`
and never loop over a set.
"""
