# Uses the project venv if it exists, so `make test` works without activating it.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: venv install test chaos check lint fix demo sim node clean

# One-time setup: create .venv and install everything (editable).
venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install -U pip
	.venv/bin/python -m pip install -e ".[dev,http,sim]"

install:
	$(PY) -m pip install -e ".[dev,http,sim]"

# P1-P5: record, fake, node, types, sync (chaos tests excluded, see addopts)
test:
	$(PY) -m pytest

# P10: 100 random partition/crash schedules must converge
chaos:
	$(PY) -m pytest -m chaos

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

# P11: a single node over HTTP
node:
	$(PY) -m hollowmesh.nodeapp

clean:
	rm -rf .pytest_cache .ruff_cache .hypothesis build dist
	find . -name __pycache__ -not -path "./.venv/*" -not -path "./venv/*" -prune -exec rm -rf {} +
