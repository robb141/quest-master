"""Compatibility shim for the v0.1 entry point.

The server now lives in ``src/quest_master/``. This file exists only so that an
MCP registration made against the old layout —

    claude mcp add quest-master -- /path/to/venv/bin/python /path/to/server.py

— keeps working. Prefer the installed console script:

    claude mcp add quest-master -- /path/to/venv/bin/quest-master
"""

from __future__ import annotations

import sys
from pathlib import Path

# Support running from a source checkout that has not been pip-installed.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from quest_master.__main__ import main  # noqa: E402 - must follow the sys.path fix

if __name__ == "__main__":
    raise SystemExit(main())
