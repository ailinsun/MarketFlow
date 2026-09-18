"""Guardian fleet-level entry guards.

Three risks exist only because entry is fanned out across many mandates; none
of them appear on a single account, and none are caught by per-mandate caps:

  1. **Self-inflicted slippage** — N mandates hitting the same thin book in the
     same tick trade against each other. Thin markets cannot absorb that: an order
     can retry for many ticks without a fill, so an unbounded fleet order is the fleet
     paying its own impact.
  2. **Order-of-service bias** — with a fixed service order the same accounts
     always get the better fill. Over many ticks that is a systematic transfer
     between mandates, which a multi-mandate service must not have.
  3. **Correlated drawdown** — every mandate can sit inside its own caps while
     the whole fleet loses together on one bad day. Per-mandate fuses cannot see
     this; only a fleet-level view can.

Everything here can only ever REFUSE or SHRINK a trade. No function in this
module can authorise one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402

# Share of a market's visible top-of-book depth the whole fleet may take in one
# tick. Conservative on purpose: taking most of the book is how a fanned-out
# strategy converts its own size into slippage.
FLEET_BOOK_SHARE = 0.25
# Fleet-wide realised loss for the current UTC day that stops all NEW positions.
# Exits are never stopped — during a bad day the safe direction must stay open.
# Expressed against the capital the whole fleet is trading, so it scales with the
# book rather than with whoever wrote it:
#
#     MARKETFLOW_FLEET_CAPITAL_USD=500000000
FLEET_CAPITAL_USD = float(os.environ.get("MARKETFLOW_FLEET_CAPITAL_USD") or 10_000_000.0)
FLEET_DAILY_DRAWDOWN_FRACTION = 0.005
FLEET_DAILY_DRAWDOWN_USD = FLEET_CAPITAL_USD * FLEET_DAILY_DRAWDOWN_FRACTION
FLEET_STATE_FILE = os.path.join(gstore.GUARDIAN_ROOT, "fleet_entry_state.json")


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def load_state(path: str = FLEET_STATE_FILE) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(doc: dict[str, Any], path: str = FLEET_STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def tenant_order(tenant_ids: list[str], *, tick_seed: Any) -> list[str]:
    """Deterministic per-tick rotation of who is served first.

    Deterministic (so a tick can be replayed and audited) but seed-dependent (so
    no tenant holds a permanent advantage). Sorting by hash rather than rotating
    the list keeps neighbours from being locked together across ticks.
    """
    seed = str(tick_seed)
    return sorted(
        tenant_ids,
        key=lambda t: hashlib.sha256(f"{seed}:{t}".encode("utf-8")).hexdigest(),
    )


def book_depth_usd(quote: dict[str, Any], levels: list[tuple[float, float]] | None = None) -> float | None:
    """Visible USD available at the ask side (top levels). None when unknown —
    callers then skip the depth guard rather than inventing a number."""
    if levels:
        return round(sum(px * sz for px, sz in levels[:3]), 6)
    ask = quote.get("best_ask")
    size = quote.get("best_ask_size")
    if ask is None or size is None:
        return None
    try:
        return round(float(ask) * float(size), 6)
    except (TypeError, ValueError):
        return None


def market_headroom_usd(
    market_id: str,
    *,
    depth_usd: float | None,
    committed_usd: float,
    share: float = FLEET_BOOK_SHARE,
) -> float | None:
    """How much more the fleet may commit to this market this tick.

    None = depth unknown => no depth-based limit applied (the per-tenant caps and
    the exchange still bound the order). 0 = the fleet's share is used up."""
    if depth_usd is None or depth_usd <= 0:
        return None
    return max(0.0, depth_usd * share - max(0.0, committed_usd))


def fleet_daily_loss_usd(state: dict[str, Any], *, now: datetime | None = None) -> float:
    day = _today(now)
    rec = (state.get("daily") or {}).get(day) or {}
    try:
        return max(0.0, float(rec.get("realized_loss_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def entry_halted(state: dict[str, Any], *, now: datetime | None = None,
                 limit_usd: float = FLEET_DAILY_DRAWDOWN_USD) -> dict[str, Any]:
    """Fleet-wide entry halt verdict. Entry only; exits are never gated here."""
    loss = fleet_daily_loss_usd(state, now=now)
    halted = loss >= limit_usd
    return {
        "halted": halted,
        "realized_loss_usd": round(loss, 6),
        "limit_usd": limit_usd,
        "day": _today(now),
        "reason": ("fleet daily realised loss reached the limit; new positions stopped "
                   "(exits unaffected)") if halted else None,
    }


def record_fleet_loss(amount_usd: float, *, state: dict[str, Any] | None = None,
                      now: datetime | None = None, path: str = FLEET_STATE_FILE) -> dict[str, Any]:
    """Add a realised loss to today's fleet total. Gains are not subtracted: the
    guard exists to stop a bad day compounding, and letting winners re-open the
    budget is exactly how a bad day compounds."""
    doc = state if state is not None else load_state(path)
    day = _today(now)
    daily = doc.setdefault("daily", {})
    rec = daily.setdefault(day, {"realized_loss_usd": 0.0})
    try:
        add = max(0.0, float(amount_usd))
    except (TypeError, ValueError):
        add = 0.0
    rec["realized_loss_usd"] = round(float(rec.get("realized_loss_usd") or 0.0) + add, 6)
    # Keep a short window; this file is a fuse, not an archive.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    for k in [k for k in daily if k < cutoff]:
        daily.pop(k, None)
    if state is None:
        save_state(doc, path)
    return doc


class TickBudget:
    """Per-tick accounting of what the fleet has already committed per market.

    Lives for one tick only — it answers "has the fleet already taken its share
    of this book right now", which is meaningless across ticks."""

    def __init__(self, share: float = FLEET_BOOK_SHARE):
        self.share = share
        self.committed: dict[str, float] = {}

    def headroom(self, market_id: str, depth_usd: float | None) -> float | None:
        return market_headroom_usd(
            str(market_id), depth_usd=depth_usd,
            committed_usd=self.committed.get(str(market_id), 0.0), share=self.share,
        )

    def commit(self, market_id: str, notional_usd: float) -> None:
        key = str(market_id)
        try:
            amt = max(0.0, float(notional_usd))
        except (TypeError, ValueError):
            return
        self.committed[key] = round(self.committed.get(key, 0.0) + amt, 6)
