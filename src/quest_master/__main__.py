"""Entry point: ``quest-master`` (or ``python -m quest_master``).

Defaults to stdio, which is how an MCP client such as Claude Code launches the
server as a subprocess. ``--transport streamable-http`` serves it over HTTP
instead, which is the path to running one shared game for several players.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quest-master", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="How clients reach the server (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port.")
    parser.add_argument("--db", help="Path to the save file (overrides $QUEST_DB).")
    parser.add_argument(
        "--seed", type=int, help="Seed the dice for reproducible runs (overrides $QUEST_SEED)."
    )
    args = parser.parse_args(argv)

    # Set before importing the server module so its lifespan picks them up.
    if args.db:
        os.environ["QUEST_DB"] = args.db
    if args.seed is not None:
        os.environ["QUEST_SEED"] = str(args.seed)

    from quest_master.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "streamable-http":
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="sse", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
