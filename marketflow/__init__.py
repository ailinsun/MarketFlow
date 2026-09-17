"""MarketFlow — execution and risk control for event-contract markets.

The package is layered so that each layer can only be reached through the one
below it:

    feeds      read-only market and chain data
    risk       risk budget, position sizing, event exposure, correlation
    execution  order construction, market gates, the trading daemon
    guardian   wallet authority, arm state, structural traps, share ledger
    monitor    settlement guards, watchdogs, operator alerts

`marketflow.paths` decides where state is written. Nothing else in the package
computes a project root.
"""
from __future__ import annotations

from marketflow.paths import PACKAGE_DIR, PROJECT_DIR, runtime_dir, runtime_path

__version__ = "0.2.0"
__all__ = ["PACKAGE_DIR", "PROJECT_DIR", "runtime_dir", "runtime_path", "__version__"]
