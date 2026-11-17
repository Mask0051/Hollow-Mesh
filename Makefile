PY ?= python

.PHONY: install test chaos demo node lint

install:
	$(PY) -m pip install -e ".[dev,http,sim]"

test:
	$(PY) -m pytest

# M7: 200 random partition/crash schedules must converge
chaos:
	$(PY) -m pytest -m chaos

# M5: run the sim on real nodes
demo:
	$(PY) -m hollowmesh.sim

# M9: a single node over HTTP
node:
	$(PY) -m hollowmesh.nodeapp

lint:
	$(PY) -m ruff check src tests
