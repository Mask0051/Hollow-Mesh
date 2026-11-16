"""M2 — FakeNode: same API as Node, instant broadcast, no crypto.

Lets MN + MP build the sim (B3) before the real protocol works. Due Mon 10 PM.
"""

from __future__ import annotations

from typing import Any

from .node import IngestResult, VersionVector


class FakeNode:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id

    def publish(self, type: str, body: dict[str, Any], day: int) -> dict[str, Any]:
        raise NotImplementedError("M2")

    def ingest(self, record: dict[str, Any]) -> IngestResult:
        raise NotImplementedError("M2")

    def version_vector(self) -> VersionVector:
        raise NotImplementedError("M2")

    def records_since(self, theirs: VersionVector) -> list[dict[str, Any]]:
        raise NotImplementedError("M2")

    def all_records(self) -> list[dict[str, Any]]:
        raise NotImplementedError("M2")
