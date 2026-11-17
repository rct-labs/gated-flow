"""Module entrypoint for `python3 -m learner`."""

from __future__ import annotations

import sys

from learner.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
