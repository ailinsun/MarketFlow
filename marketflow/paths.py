"""Where MarketFlow reads code from and where it writes state to.

Every module in the package resolves paths through this one file. Two reasons it
is not left to each module:

  * hand-counted `os.path.dirname` chains silently point at the wrong directory
    the moment a file moves, and nothing fails loudly when they do;
  * an installed package must never write state beside its own source. Set
    `MARKETFLOW_RUNTIME` and every artefact — ledgers, arm state, caches,
    heartbeats — moves with it.
"""
from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)

__all__ = ["PACKAGE_DIR", "PROJECT_DIR", "runtime_dir", "runtime_path"]


def runtime_dir() -> str:
    """Root of the mutable state tree, `MARKETFLOW_RUNTIME` or `<project>/runtime`.

    Read on every call rather than captured at import time, so a test or a
    self-test can point the whole package at a temporary directory.
    """
    return os.environ.get("MARKETFLOW_RUNTIME") or os.path.join(PROJECT_DIR, "runtime")


def runtime_path(*parts: str) -> str:
    """A path inside the runtime tree. Creates nothing; callers own their own mkdir."""
    return os.path.join(runtime_dir(), *parts)
