#!/usr/bin/env python3
"""Read-only Polygon chain primitives — the single source of on-chain reads.

Why it exists:

1. **A balance can only come from the chain.** An indexer serves a snapshot, and
   snapshots have been measured lagging the chain by the better part of an hour.
   During that window an indexer keeps reporting money that has already been
   spent as available, a UI shows that number to somebody, and sizing keeps
   spending against it. Any question of the form "how much is there right now"
   has exactly one answer: `eth_call`.

   Indexers remain the right tool for "what happened" — transfer history, deposit
   detection — where arriving late only means being told late.

2. **The RPC endpoint list and the collateral contract address used to be written
   out in four separate modules**, so changing one left three stale. They live
   here now and consumers import rather than copy.

Boundary: this module reads (`eth_call` and constants). It has no signing, sends
no transaction and holds no private key, so any surface can import it safely.
Standard library only, no third-party dependency.

selftest: python3 chain.py --selftest   (fully offline, opener injected)
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any, Callable

# Public RPC endpoints, tried in order. Several well-known ones are deliberately
# absent: some return 401 without a key, some rate-limit anonymous callers hard
# enough to be useless as a fallback. Verify before adding one back.
POLYGON_RPCS: tuple[str, ...] = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
)

# The venue's collateral token and both USDC variants on Polygon (all 6 decimals).
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"     # Polymarket CollateralToken
USDC = "0x3c499c542cef5E3811e1192cE70d8cC03d5c3359"     # native USDC
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"    # bridged USDC.e
TOKEN_DECIMALS = 6

ERC20_BALANCE_OF = "0x70a08231"     # keccak("balanceOf(address)")[:4]
RPC_TIMEOUT_SEC = 12
USER_AGENT = "marketflow-chain/0.1"      # some endpoints reject the default urllib UA


def _rpc_call(rpc: str, body: bytes, *, timeout: int = RPC_TIMEOUT_SEC) -> Any:
    req = urllib.request.Request(
        rpc, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def erc20_balance(address: str, *, token: str = PUSD,
                  rpcs: tuple[str, ...] = POLYGON_RPCS,
                  opener: Callable[[str, bytes], Any] | None = None) -> float | None:
    """Balance of `token` held by `address`, in human units (6 decimals).

    **None means "not known"** — a malformed address, every RPC unreachable, or an
    unparseable response. A failed read is never folded into 0.0: for a balance,
    0 is an assertion ("there is no money here"), and callers must treat the two
    differently. A display shows an em dash for None; sizing falls back to
    cap-only rather than concluding the account is empty.
    """
    addr = str(address or "").strip()
    if not (addr.startswith("0x") and len(addr) == 42):
        return None
    data = ERC20_BALANCE_OF + "0" * 24 + addr[2:].lower()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": token, "data": data}, "latest"]}).encode()
    call = opener or _rpc_call
    for rpc in rpcs:
        try:
            doc = call(rpc, body)
            res = (doc or {}).get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return round(int(res, 16) / 10 ** TOKEN_DECIMALS, 6)
        except Exception:
            continue    # try the next RPC; None only when all of them fail
    return None


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    seen: list[str] = []

    def ok(rpc: str, body: bytes) -> Any:
        seen.append(rpc)
        p = json.loads(body)
        c = p["params"][0]
        shape_ok = (p["method"] == "eth_call" and c["to"] == PUSD
                    and c["data"].startswith(ERC20_BALANCE_OF)
                    and len(c["data"]) == 10 + 64
                    and c["data"].endswith("eeee000000000000000000000000000000000001"))
        return {"result": hex(379311)} if shape_ok else {"error": "bad call shape"}

    check("eth_call shape is correct and 6-decimal decoding works",
          erc20_balance("0xEeEe000000000000000000000000000000000001", opener=ok) == 0.379311)
    check("stops at the first success, no extra RPC calls", len(seen) == 1)

    tried: list[str] = []

    def dead_first(rpc: str, body: bytes) -> Any:
        tried.append(rpc)
        if len(tried) == 1:
            raise OSError("rpc down")
        return {"result": hex(1_500_000)}

    check("first RPC down -> falls back to the second",
          erc20_balance("0x" + "a" * 40, opener=dead_first) == 1.5 and len(tried) == 2)
    check("all RPCs down -> None, never 0",
          erc20_balance("0x" + "a" * 40, rpcs=("x", "y"),
                        opener=lambda r, b: (_ for _ in ()).throw(OSError("x"))) is None)
    check("RPC returns an error -> None, never 0",
          erc20_balance("0x" + "a" * 40, rpcs=("x",),
                        opener=lambda r, b: {"error": {"code": -32000}}) is None)
    check("non-hex result -> None",
          erc20_balance("0x" + "a" * 40, rpcs=("x",),
                        opener=lambda r, b: {"result": None}) is None)
    check("malformed address -> None, and no network call",
          erc20_balance("nope", opener=lambda r, b: 1 / 0) is None
          and erc20_balance("", opener=lambda r, b: 1 / 0) is None
          and erc20_balance("0x123", opener=lambda r, b: 1 / 0) is None)
    check("a real zero balance is 0.0, not None",
          erc20_balance("0x" + "a" * 40, rpcs=("x",),
                        opener=lambda r, b: {"result": "0x0"}) == 0.0)
    check("another token can be specified (USDC / USDC.e)",
          erc20_balance("0x" + "a" * 40, token=USDC_E, rpcs=("x",),
                        opener=lambda r, b: ({"result": hex(2_000_000)}
                                             if json.loads(b)["params"][0]["to"] == USDC_E
                                             else {"error": "wrong token"})) == 2.0)
    check("constants exist and are well formed",
          all(a.startswith("0x") and len(a) == 42 for a in (PUSD, USDC, USDC_E))
          and len(POLYGON_RPCS) >= 2)

    print("selftest:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print("chain.py - read-only Polygon primitives. Run with --selftest.")
