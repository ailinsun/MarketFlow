#!/usr/bin/env python3
"""Read a UMA optimistic-oracle proposal directly from an EVM node (research CLI).

This is the command-line face of `marketflow.monitor.uma_onchain`, which holds the
single implementation: a dependency-free keccak-256, ABI encoding for the calls the
adapter and oracle expose, and a reader that returns the proposed outcome, its
timestamp and dispute state. The settlement guard in `marketflow.monitor` calls the
same code path, so the number a report cites and the number the runtime acts on
cannot drift apart.

The path `instruments/uma_onchain.py` is kept because published reports cite it.

    python3 -B instruments/uma_onchain.py --selftest
    python3 -B instruments/uma_onchain.py --condition-id 0x... --rpc https://...

An RPC endpoint is required for a live read; `--selftest` runs entirely offline.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from marketflow.monitor.uma_onchain import (  # noqa: E402
    OnchainError, eth_call, fetch_uma_proposal, keccak256, main, selftest,
)

__all__ = ["OnchainError", "eth_call", "fetch_uma_proposal", "keccak256", "selftest"]

if __name__ == "__main__":
    main()
