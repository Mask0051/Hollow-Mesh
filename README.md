# Hollow Mesh

Patriot HackFest Fall 2026 · Track: Life Underground

A mesh of signed, append-only feeds that gossip between nodes, so messages survive network cuts,
blackouts, and lying relays. **Countertally** runs on top and counts health reports so residents can
compare them with the Directorate's official numbers.

## Layout

| Path | Owner | What |
|---|---|---|
| `src/hollowmesh/protocol/` | MK | Records, signing, node ingest, gossip sync, transports (P1–P7) |
| `src/hollowmesh/services/` | MK | Countertally and other services (P8) |
| `src/hollowmesh/nodeapp.py` | MK | Runs one node over HTTP for the real-laptop segment (P11) |
| `src/hollowmesh/sim/` | Teammate | The simulated Hollow: residents, stairwells, Directorate attacks (H2–H10) |
| `spec/` | all | `world.json` (H2); frozen copy of the spec (Doc 2 §1) |
| `web/` | Teammate | Vite + React dashboard (H3, H6–H9) |
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
| `make demo` | `python -m hollowmesh.sim` |
| `make node` | `python -m hollowmesh.nodeapp` |
