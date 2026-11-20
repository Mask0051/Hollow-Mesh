# Uses the project venv if it exists, so `make test` works without activating it.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: venv install test check lint fix demo sim hosted node metrics clean

# One-time setup: create .venv and install everything (editable).
venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install -U pip
	.venv/bin/python -m pip install -e ".[dev,http,sim]"

install:
	$(PY) -m pip install -e ".[dev,http,sim]"

# Everything: protocol, services, sim, metrics (fast; ~15 s)
test:
	$(PY) -m pytest

# Run before opening a PR
check: lint test

lint:
	$(PY) -m ruff check src tests

fix:
	$(PY) -m ruff check --fix src tests

# P6: the sim on real nodes, live at http://localhost:8000 (sim + dashboard: teammate)
# demo = locked seed + scripted attacks (H10); sim = free play with the control panel
demo:
	$(PY) -m hollowmesh.sim --script

sim:
	$(PY) -m hollowmesh.sim

# The public demo as Render runs it (render.yaml): one private sim per visitor
hosted:
	$(PY) -m hollowmesh.sim --hosted --script --residents 150

# P9: measured results over 10 seeds -> results.json + README charts (same seeds, same files)
metrics:
	$(PY) -m hollowmesh.sim.metrics --out results.json
	$(PY) -m hollowmesh.sim.plots results.json docs/img

# P11 (not built for this submission): a single node over HTTP
node:
	$(PY) -m hollowmesh.nodeapp

clean:
	rm -rf .pytest_cache .ruff_cache .hypothesis build dist
	find . -name __pycache__ -not -path "./.venv/*" -not -path "./venv/*" -prune -exec rm -rf {} +
