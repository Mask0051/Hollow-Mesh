"""P11 — Run one node over HTTP (FastAPI, Doc 2 §1.5) for the real-laptop segment.

`python -m hollowmesh.nodeapp --port 8000 --peers http://192.168.1.x:8000,...`
Peers come from --peers / PEERS env var plus POST /peers. Gossip runs in a background thread.
"""


def main() -> None:
    raise SystemExit("nodeapp not implemented yet (P11)")


if __name__ == "__main__":
    main()
