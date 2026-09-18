"""MarketFlow — execution and risk control for event-contract markets.

Data flows one way, and authority is a separate axis:

    feeds      read-only market data, large prints, tape rotation
    risk       exposure, capital-relative budgets, sizing, structural gates
    execution  market gates, order construction and fuses, the exit-first daemon
    guardian   delegated mandates: authority proofs, arm state, enclave signing
    monitor    settlement guards, heartbeat watchdogs, operator alerts
    mcp        a read-only data plane over the same modules

`marketflow.paths` decides where state is written. Nothing else in the package
computes a project root.
"""
from __future__ import annotations

from marketflow.paths import PACKAGE_DIR, PROJECT_DIR, runtime_dir, runtime_path

__version__ = "0.2.1"
__all__ = ["PACKAGE_DIR", "PROJECT_DIR", "runtime_dir", "runtime_path", "__version__"]
