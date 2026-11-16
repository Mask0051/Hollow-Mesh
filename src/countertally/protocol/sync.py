"""M4 — Version-vector gossip sync.

Messages:
  request  {vv}            "here's what I have"
  response {records}       "here's what you're missing"
  push     {records}       sent on publish to reachable peers

Merge rule: a record is applied only through Node.ingest, never written directly.
Write the version-vector merge tests yourself first (Doc 4 §2).
"""

from __future__ import annotations

from typing import Any

from .node import Node


def handle_message(node: Node, msg: dict[str, Any]) -> dict[str, Any] | None:
    """Apply an incoming sync message; return a reply message if one is needed."""
    raise NotImplementedError("M4")


def sync_pair(a: Node, b: Node) -> int:
    """One full request/response exchange in both directions. Returns records moved."""
    raise NotImplementedError("M4")
