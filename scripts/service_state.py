#!/usr/bin/env python3
"""Classify a running local Chatbot service for run-browser.sh.

Usage: service_state.py HEALTH_URL CHECKOUT_ROOT

Prints one word:

  current      the service runs CHECKOUT_ROOT's code as it is on disk now
  stale        it runs CHECKOUT_ROOT's code, but the files have changed since
               it started
  foreign      it runs from another checkout or worktree
  unknown      it answered without a fingerprint (predates this contract)
  unreachable  nothing answered within the timeout

Standard library only: the launcher calls this before it knows whether the
checkout's virtualenv is usable.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


def classify(info: Any, root: str) -> str:
    if not isinstance(info, dict) or not info.get("fingerprint"):
        return "unknown"
    if info.get("source") != root:
        return "foreign"
    if info.get("stale"):
        return "stale"
    return "current"


def probe(url: str, root: str, timeout: float = 2.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            info = json.load(response)
    except Exception:
        return "unreachable"
    return classify(info, root)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(probe(argv[1], argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
