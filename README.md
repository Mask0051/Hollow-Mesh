# Hollow Mesh

Patriot HackFest Fall 2026 · Track: Life Underground

A mesh of signed, append-only feeds that gossip between nodes, so messages survive network cuts,
blackouts, and lying relays. **Countertally** runs on top and counts health reports so residents can
compare them with the Directorate's official numbers.

## Layout

| Path | Owner | What |
|---|---|---|
| `src/countertally/protocol/` | MK | Records, signing, node ingest, gossip sync, transports |
| `src/countertally/nodeapp.py` | MK | Runs one node over HTTP (M9) |
| `src/countertally/sim/` | MN + MP | The simulated Hollow: residents, stairwells, Directorate attacks |
| `src/countertally/services/` | MN + MP | Countertally and other services |
| `spec/` | all | `world.json` (B1); event spec |
| `fixtures/`, `tools/` | MN + MP | Replay fixture + `replay_ws.py` (B2) |
| `web/` | MP | Vite + React dashboard (C1+) |
| `tests/` | all | pytest + Hypothesis |
| `Docs/` | all | Planning docs |

## Setup

```sh
python -m venv .venv
# macOS/Linux: source .venv/bin/activate    Windows: .venv\Scripts\activate
pip install -e ".[dev,http,sim]"
```

## Commands

| `make` | Equivalent without make |
|---|---|
| `make test` | `python -m pytest` |
| `make chaos` | `python -m pytest -m chaos` |
| `make demo` | `python -m countertally.sim` |
| `make node` | `python -m countertally.nodeapp` |
