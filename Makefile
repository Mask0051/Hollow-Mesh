PY ?= python

.PHONY: install test chaos demo node lint

install:
	$(PY) -m pip install -e ".[dev,http,sim]"

test:
	$(PY) -m pytest

# P10: 100 random partition/crash schedules must converge
chaos:
	$(PY) -m pytest -m chaos

# P6: run the sim on real nodes
demo:
	$(PY) -m hollowmesh.sim

# P11: a single node over HTTP
node:
	$(PY) -m hollowmesh.nodeapp

lint:
	$(PY) -m ruff check src tests
