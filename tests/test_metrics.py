"""P9: the metrics runners, on a small Hollow (fast). `make metrics` runs the real thing."""

from __future__ import annotations

import pytest

from hollowmesh.sim import metrics


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(metrics, "RESIDENTS", 80)


def test_sos_reaches_every_working_phone_with_wifi_cut():
    r = metrics.delivery(seed=1, wifi=False, off=0.5)
    assert r["delivered"] == 1.0 and r["t_levels"] is not None


def test_tampered_copies_are_never_accepted():
    r = metrics.tamper(seed=1)
    assert r["altered_sent"] > 0 and r["altered_accepted"] == 0


def test_split_heals_and_converges():
    r = metrics.heal(seed=1)
    assert r["sync_at_heal"] < 1.0 and r["t_converge"] is not None


def test_same_seed_same_numbers_and_summary_renders():
    a, b = metrics.delivery(2, False, 0.25), metrics.delivery(2, False, 0.25)
    assert a == b
    assert metrics._summ([None, 2, 4], minutes=True) == {
        "runs": 3, "never": 1, "mean": 30.0, "median": 30.0, "min": 20, "max": 40}
