"""Local web UI for configuring and running the AlgoTest sweep.

    uv run python webapp.py            # http://127.0.0.1:8765
    uv run python webapp.py --port 9000
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    # Bound to loopback only - this drives your logged-in AlgoTest session and should
    # never be reachable from outside this machine.
    uvicorn.run("src.web.app:app", host="127.0.0.1", port=args.port, reload=False)


if __name__ == "__main__":
    main()
