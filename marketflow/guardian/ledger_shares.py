#!/usr/bin/env python3
"""Unitized share ledger — pure accounting. It performs no money operation.

This module implements the **accounting mechanism** and prescribes no business
model: how to charge (a flat fee, a performance share, nothing at all) is the
deployment's decision and lives elsewhere. What is here is unitized fund
accounting — NAV, high-water mark, subscriptions and redemptions — plus an
optional performance-fee crystallisation.

The physical structure does not change: every holder has their own isolated
wallet, and this ledger accounts for each one in its own segregated pool. Pooling
is a separate stage behind `POOLED_FUND_GATE`.

Three boundaries are enforced by the code rather than by comment:

  1. **No money operation.** It does not sign, does not transfer and touches no
     private key. It imports no executor, arm, order or wallet module, and has no
     path that writes arm-state, caps or a gate file. It reads cash-flow facts and
     writes a ledger. That is all.

  2. **It does not change the isolation structure.** A pool in segregated mode
     **refuses a second holder**: today each holder is their own single pool,
     their share is always 100%, and the ledger's conclusion equals the real
     balance in their wallet. Pooled mode is the same mathematics under a
     different configuration; switching to it requires an explicit open_pool and
     never happens by default.

  3. **Crystallisation issues an invoice and moves nothing.** Settling a period's
     performance fee does exactly two things: write a receivable row and ratchet
     the high-water mark up. NAV, shares and funds are untouched. The fee is a
     receivable collected out of band through a payment channel — this module
     could not move the money in a custodial wallet even if asked to.

The accounting (standard unitized fund accounting):

    nav_per_share = nav_usd / shares_outstanding   derived, never persisted,
                                                   because a persisted copy drifts
    subscribe  shares_issued = cash / nav_per_share(before)  -> no dilution of
                                                              existing holders
    redeem     cash          = shares_burned * nav_per_share
    return     issues no shares. A rising NAV raises value per share, which
               attributes pro rata automatically. A distribution event only
               records that attribution explicitly, for audit; it does not
               advance state.

Rounding always favours the pool — shares issued round down, shares burned round
up — so the pool is never short by a rounding cent. The dust that produces stays
in `residual_usd` rather than disappearing.

Amounts and share counts in events are encoded as **strings** ("24.190000"). A
ledger cannot survive float round-tripping; consumers read them back with
Decimal(row["cash_usd"]).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402  the single source of ledger persistence
#                                      (flock plus append-only)

SCHEMA_VERSION = "guardian-shares-v0.1"
SHARES_ROOT = os.path.join(gstore.GUARDIAN_ROOT, "shares")

# The stablecoins involved carry 6 decimals on-chain, so quantising cash to 6 dp
# is exactly the chain's own precision. Shares use 12 dp to leave room for NAV
# movement. The context precision is for intermediate division only and is higher
# than anything ever persisted.
CASH_DP = Decimal("0.000001")
SHARE_DP = Decimal("0.000000000001")
CTX = Context(prec=34)
INITIAL_NAV_PER_SHARE = Decimal("1")
# Every accepted stablecoin is booked at $1. A depeg is not modelled here; a real
# one would need its own accounting decision.
STABLE_PAR_USD = Decimal("1")
FUNDING_TOKENS = ("USDC", "USDC.e", "pUSD")
# One deposit can appear twice, once per token: the source arriving and then the
# wrapped collateral. Two rows at the same address and amount inside this window
# are the same money, and counting both would issue shares out of nothing.
WRAP_PAIR_WINDOW_SEC = 3600.0

MODES = ("segregated", "pooled")
_POOL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Stage 1 is a per-holder performance fee on each holder's own segregated pool and
# needs no gate at all. This gate governs **stage 2, physical pooling**.
#
# Pooling changes the legal character of the service: in most jurisdictions a
# pooled vehicle looks more like a collective investment scheme than a custody
# tool. It is therefore off by default, and turning it on requires the deployment's
# explicit approval plus qualified legal advice. This is not a switch to flip and
# regularise afterwards. The threshold below is a placeholder; set it for your own
# jurisdiction and contractual structure.
POOLED_FUND_GATE = {
    "applies_to": "physical_pooling_only",
    "pool_aum_usd_min": 100000.0,
    "requires_owner_approval": True,
    "requires_contract_rewrite": "service agreement -> collective investment vehicle (obtain legal advice)",
}

# Performance fee rate in basis points. The default is 10%; set your own. This
# constant carries both the ledger mathematics and the basis of every invoice
# already issued, so changing it changes whether past periods still recompute.
# Decide how historical periods are handled before touching it.
CARRY_RATE_BPS = 1000


class LedgerError(Exception):
    pass


def _q(value, dp: Decimal, rounding: str) -> Decimal:
    try:
        return Decimal(str(value)).quantize(dp, rounding=rounding)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise LedgerError(f"not a usable decimal: {value!r}") from exc


def q_cash(value) -> Decimal:
    return _q(value, CASH_DP, ROUND_HALF_EVEN)


def q_shares_issue(value) -> Decimal:
    """Round shares issued down: issue slightly fewer rather than leave the pool
    short by a rounding cent."""
    return _q(value, SHARE_DP, ROUND_FLOOR)


def q_shares_burn(value) -> Decimal:
    """Round shares burned up: burn slightly more rather than let a redeemer take
    an extra rounding cent."""
    return _q(value, SHARE_DP, ROUND_CEILING)


def segregated_pool_id(tenant_id: str) -> str:
    return f"seg_{tenant_id}"


@dataclass
class TenantPosition:
    tenant_id: str
    shares: Decimal = Decimal(0)
    contributed_usd: Decimal = Decimal(0)
    redeemed_usd: Decimal = Decimal(0)
    # At subscription this is the share-weighted average entry value; each
    # crystallisation ratchets it up to the value per share at that moment.
    hwm_per_share: Decimal = INITIAL_NAV_PER_SHARE
    first_event_ts: str | None = None


@dataclass
class PoolState:
    pool_id: str
    mode: str = "segregated"
    nav_usd: Decimal = Decimal(0)
    shares_outstanding: Decimal = Decimal(0)
    tenants: dict[str, TenantPosition] = field(default_factory=dict)
    seq: int = 0
    last_ts: str | None = None
    last_mark_ts: str | None = None
    opened_at: str | None = None
    carry_accrued_usd: Decimal = Decimal(0)      # cumulative crystallised receivable,
    #                                              collected out of band
    consumed_sources: set[str] = field(default_factory=set)

    @property
    def nav_per_share(self) -> Decimal:
        """Derived. An empty pool falls back to the initial value per share, so a
        subscription after a full redemption starts again at 1.0."""
        if self.shares_outstanding <= 0:
            return INITIAL_NAV_PER_SHARE
        return CTX.divide(self.nav_usd, self.shares_outstanding)

    def equity_usd(self, tenant_id: str) -> Decimal:
        t = self.tenants.get(tenant_id)
        if t is None:
            return Decimal(0)
        return q_cash(t.shares * self.nav_per_share)

    def residual_usd(self) -> Decimal:
        """NAV minus the sum of holder equity: rounding dust, invariantly >= 0."""
        return q_cash(self.nav_usd - sum((self.equity_usd(t) for t in self.tenants), Decimal(0)))

    def summary(self) -> dict:
        return {
            "pool_id": self.pool_id, "mode": self.mode, "seq": self.seq,
            "nav_usd": str(q_cash(self.nav_usd)),
            "shares_outstanding": str(q_shares_issue(self.shares_outstanding)),
            "nav_per_share": str(_q(self.nav_per_share, SHARE_DP, ROUND_HALF_EVEN)),
            "residual_usd": str(self.residual_usd()),
            "carry_accrued_usd": str(q_cash(self.carry_accrued_usd)),
            "opened_at": self.opened_at,
            "last_ts": self.last_ts, "last_mark_ts": self.last_mark_ts,
            "tenants": {
                tid: {
                    "shares": str(t.shares), "equity_usd": str(self.equity_usd(tid)),
                    "first_event_ts": t.first_event_ts,
                    "contributed_usd": str(t.contributed_usd),
                    "redeemed_usd": str(t.redeemed_usd),
                    "pnl_usd": str(q_cash(self.equity_usd(tid) + t.redeemed_usd - t.contributed_usd)),
                    "hwm_per_share": str(_q(t.hwm_per_share, SHARE_DP, ROUND_HALF_EVEN)),
                }
                for tid, t in sorted(self.tenants.items())
            },
        }


def _source_id(source: dict | None) -> str | None:
    if not source:
        return None
    key = source.get("key") or source.get("tx")
    if not key:
        return None
    return f"{source.get('ledger') or 'unknown'}:{key}"


def _check_ts(state: PoolState, at: str) -> None:
    if not at:
        raise LedgerError("event needs a timestamp")
    if state.last_ts and at < state.last_ts:
        # Allowing out-of-order events would price an earlier subscription at a
        # later NAV. Backdated pricing makes the whole ledger untrustworthy.
        raise LedgerError(f"out-of-order event {at} < last {state.last_ts}")


def _event(state: PoolState, kind: str, at: str, **fields) -> dict:
    return {"schema_version": SCHEMA_VERSION, "seq": state.seq + 1, "ts": at,
            "kind": kind, "pool_id": state.pool_id, **fields}


# ------------------------------------------------- event construction and replay

def open_pool(pool_id: str, *, mode: str = "segregated", at: str | None = None) -> dict:
    if not _POOL_ID_RE.match(pool_id or ""):
        raise LedgerError(f"unsafe pool_id {pool_id!r}")
    if mode not in MODES:
        raise LedgerError(f"unknown pool mode {mode!r}")
    at = at or gstore.iso_now()
    return {"schema_version": SCHEMA_VERSION, "seq": 1, "ts": at, "kind": "pool_open",
            "pool_id": pool_id, "mode": mode,
            "initial_nav_per_share": str(INITIAL_NAV_PER_SHARE)}


def subscribe(state: PoolState, tenant_id: str, cash_usd, *, at: str,
              source: dict | None = None) -> dict:
    """Convert a subscription into shares, priced at the value per share **before**
    it — so no existing holder is diluted."""
    _check_ts(state, at)
    cash = q_cash(cash_usd)
    if cash <= 0:
        raise LedgerError(f"subscription must be positive, got {cash}")
    if state.mode == "segregated" and tenant_id not in state.tenants and state.tenants:
        raise LedgerError(
            f"pool {state.pool_id} is segregated and already belongs to "
            f"{next(iter(state.tenants))}; pooling requires an explicit "
            f"open_pool(mode='pooled')")
    price = state.nav_per_share
    shares = q_shares_issue(CTX.divide(cash, price))
    if shares <= 0:
        raise LedgerError(f"subscription {cash} too small to issue shares at {price}")
    return _event(state, "subscribe", at, tenant_id=tenant_id, cash_usd=str(cash),
                  nav_per_share=str(_q(price, SHARE_DP, ROUND_HALF_EVEN)),
                  shares_issued=str(shares),
                  priced_off_mark_ts=state.last_mark_ts, source=source or {})


def redeem(state: PoolState, tenant_id: str, *, shares=None, cash_usd=None,
           at: str, source: dict | None = None) -> dict:
    """Redeem: either shares -> cash or cash -> shares, one or the other."""
    _check_ts(state, at)
    if (shares is None) == (cash_usd is None):
        raise LedgerError("redeem takes exactly one of shares= / cash_usd=")
    pos = state.tenants.get(tenant_id)
    if pos is None:
        raise LedgerError(f"{tenant_id} holds no shares in {state.pool_id}")
    price = state.nav_per_share
    if shares is not None:
        burned = q_shares_burn(shares)
        cash = q_cash(burned * price)
    else:
        cash = q_cash(cash_usd)
        burned = q_shares_burn(CTX.divide(cash, price))
    if burned <= 0 or cash <= 0:
        raise LedgerError("redemption must be positive")
    if burned > pos.shares:
        raise LedgerError(f"{tenant_id} holds {pos.shares} shares, cannot redeem {burned}")
    return _event(state, "redeem", at, tenant_id=tenant_id, cash_usd=str(cash),
                  nav_per_share=str(_q(price, SHARE_DP, ROUND_HALF_EVEN)),
                  shares_burned=str(burned),
                  priced_off_mark_ts=state.last_mark_ts, source=source or {})


def mark(state: PoolState, nav_usd, *, at: str, source: dict | None = None) -> tuple[dict, dict | None]:
    """A pool valuation point -> (mark event, distribution event or None).

    The share register is constant between two valuations — a subscription or a
    redemption is itself an event and breaks the interval — so each holder's PnL
    is exactly shares * delta(value per share), and the sum is exactly delta(NAV).
    The conservation in a distribution is constructed, not approximated. The
    quantisation remainder goes to the largest holder, which is the usual asset
    management convention and keeps the sum exact to the cent.
    """
    _check_ts(state, at)
    nav_new = q_cash(nav_usd)
    if nav_new < 0:
        raise LedgerError(f"pool NAV cannot be negative: {nav_new}")
    price_before = state.nav_per_share
    delta = q_cash(nav_new - state.nav_usd)
    mark_ev = _event(state, "mark", at, nav_usd=str(nav_new),
                     nav_usd_before=str(q_cash(state.nav_usd)),
                     shares_outstanding=str(state.shares_outstanding),
                     nav_per_share=str(_q(
                         CTX.divide(nav_new, state.shares_outstanding)
                         if state.shares_outstanding > 0 else INITIAL_NAV_PER_SHARE,
                         SHARE_DP, ROUND_HALF_EVEN)),
                     pnl_usd=str(delta), source=source or {})
    if delta == 0 or not state.tenants:
        return mark_ev, None
    if state.shares_outstanding <= 0:
        dist = _event(state, "distribution", at, period_start=state.last_mark_ts,
                      pnl_usd=str(delta), allocations=[], unallocated_usd=str(delta))
        dist["seq"] = state.seq + 2
        return mark_ev, dist
    price_after = CTX.divide(nav_new, state.shares_outstanding)
    holders = sorted(state.tenants.values(), key=lambda t: (-t.shares, t.tenant_id))
    allocations, running = [], Decimal(0)
    for t in holders[1:]:
        amt = q_cash(t.shares * (price_after - price_before))
        running += amt
        allocations.append({"tenant_id": t.tenant_id, "shares": str(t.shares),
                            "pnl_usd": str(amt)})
    allocations.insert(0, {"tenant_id": holders[0].tenant_id, "shares": str(holders[0].shares),
                           "pnl_usd": str(q_cash(delta - running))})
    dist = _event(state, "distribution", at, period_start=state.last_mark_ts,
                  nav_per_share_before=str(_q(price_before, SHARE_DP, ROUND_HALF_EVEN)),
                  nav_per_share_after=str(_q(price_after, SHARE_DP, ROUND_HALF_EVEN)),
                  pnl_usd=str(delta), allocations=allocations, unallocated_usd="0")
    dist["seq"] = state.seq + 2
    return mark_ev, dist


def apply(state: PoolState, event: dict) -> PoolState:
    """Advance state. One cash-flow fact is counted once, so replaying the ledger
    never issues shares twice."""
    kind = event.get("kind")
    if event.get("pool_id") != state.pool_id and kind != "pool_open":
        raise LedgerError(f"event for pool {event.get('pool_id')!r} applied to {state.pool_id!r}")
    ts = event.get("ts")
    if kind in ("subscribe", "redeem"):
        sid = _source_id(event.get("source"))
        if sid and sid in state.consumed_sources:
            raise LedgerError(f"duplicate source {sid} - one cash-flow fact cannot be booked twice")

    if kind == "pool_open":
        state.mode = event.get("mode", state.mode)
        state.opened_at = ts
    elif kind == "subscribe":
        tid = event["tenant_id"]
        if state.mode == "segregated" and tid not in state.tenants and state.tenants:
            raise LedgerError(f"segregated pool {state.pool_id} refuses a second tenant")
        pos = state.tenants.setdefault(tid, TenantPosition(tenant_id=tid, first_event_ts=ts))
        cash, shares = Decimal(event["cash_usd"]), Decimal(event["shares_issued"])
        price = Decimal(event["nav_per_share"])
        total = pos.shares + shares
        pos.hwm_per_share = (CTX.divide(pos.hwm_per_share * pos.shares + price * shares, total)
                             if total > 0 else price)
        pos.shares = total
        pos.contributed_usd += cash
        state.shares_outstanding += shares
        state.nav_usd += cash
    elif kind == "redeem":
        tid = event["tenant_id"]
        pos = state.tenants.get(tid)
        if pos is None:
            raise LedgerError(f"redeem for unknown tenant {tid}")
        cash, burned = Decimal(event["cash_usd"]), Decimal(event["shares_burned"])
        if burned > pos.shares:
            raise LedgerError(f"redeem {burned} exceeds {tid} holding {pos.shares}")
        pos.shares -= burned
        pos.redeemed_usd += cash
        state.shares_outstanding -= burned
        state.nav_usd -= cash
    elif kind == "mark":
        state.nav_usd = Decimal(event["nav_usd"])
        state.last_mark_ts = ts
    elif kind == "distribution":
        pass                       # audit record only; the PnL already rode in on
        #                            the mark's NAV
    elif kind == "carry_crystallized":
        # Ratchet the high-water mark only. NAV and shares are untouched: the fee
        # is a receivable collected out of band.
        price = Decimal(event["nav_per_share"])
        for ln in event.get("lines") or []:
            pos = state.tenants.get(ln["tenant_id"])
            if pos is None:
                raise LedgerError(f"carry line for unknown tenant {ln['tenant_id']}")
            if price > pos.hwm_per_share:
                pos.hwm_per_share = price
        state.carry_accrued_usd += Decimal(event["carry_total_usd"])
    else:
        raise LedgerError(f"unknown event kind {kind!r}")

    if kind in ("subscribe", "redeem"):
        sid = _source_id(event.get("source"))
        if sid:
            state.consumed_sources.add(sid)
    state.seq = max(state.seq, int(event.get("seq") or 0))
    state.last_ts = ts or state.last_ts
    return state


def replay(events: list[dict]) -> PoolState:
    if not events:
        raise LedgerError("cannot replay an empty ledger")
    head = events[0]
    if head.get("kind") != "pool_open":
        raise LedgerError("ledger must start with pool_open")
    state = PoolState(pool_id=head["pool_id"], mode=head.get("mode", "segregated"))
    for ev in events:
        apply(state, ev)
    return state


# ------------------------------- performance fee (computed here, collected elsewhere)

def carry_preview(state: PoolState, *, rate_bps: int = CARRY_RATE_BPS) -> dict:
    """Accrued performance fee against the high-water mark. **Read-only: it does
    not advance state** — booking it goes through crystallize_carry.

    The fee base is the amount by which value per share exceeds that holder's
    high-water mark, times their shares. Using value per share against the mark,
    rather than a change in NAV, is mandatory: a subscription raises NAV without
    being profit, and charging on a NAV delta would charge a holder on their own
    principal.
    """
    price = state.nav_per_share
    rate = CTX.divide(Decimal(int(rate_bps)), Decimal(10000))
    lines, total = [], Decimal(0)
    for tid, pos in sorted(state.tenants.items()):
        gain_per_share = price - pos.hwm_per_share
        gain = q_cash(pos.shares * gain_per_share) if gain_per_share > 0 else Decimal(0)
        fee = q_cash(gain * rate)
        total += fee
        lines.append({"tenant_id": tid, "gain_above_hwm_usd": str(gain),
                      "carry_usd": str(fee)})
    return {"accrued_only": True, "rate_bps": int(rate_bps),
            "nav_per_share": str(_q(price, SHARE_DP, ROUND_HALF_EVEN)),
            "carry_total_usd": str(total), "per_tenant": lines,
            "pooled_fund_gate": dict(POOLED_FUND_GATE),
            "note": "accrual only; this function books nothing (crystallize_carry "
                    "does), and no path in this ledger touches holder funds - "
                    "collection happens out of band"}


def crystallize_carry(state: PoolState, *, at: str, rate_bps: int = CARRY_RATE_BPS,
                      period: str | None = None) -> dict | None:
    """Settle one period: write a receivable row and ratchet the high-water mark.
    **NAV, shares and funds are all untouched.**

    The fee is a **receivable**. Funds in a custodial wallet cannot be moved from
    here at all, so collection happens out of band. Crystallising therefore does
    exactly two things: record what is owed for the period, and raise the mark to
    the current value per share.

    The ratchet is what guarantees the same gain is never charged twice: a loss
    followed by a recovery produces no new fee, because the holder must first
    regain the old high.

    Returns None when the period accrues nothing — everyone below their own mark —
    rather than writing an empty invoice row.
    """
    _check_ts(state, at)
    price = state.nav_per_share
    rate = CTX.divide(Decimal(int(rate_bps)), Decimal(10000))
    lines, total = [], Decimal(0)
    for tid, pos in sorted(state.tenants.items()):
        if pos.shares <= 0 or price <= pos.hwm_per_share:
            continue
        gain = q_cash(pos.shares * (price - pos.hwm_per_share))
        fee = q_cash(gain * rate)
        if fee <= 0:
            continue
        total += fee
        lines.append({"tenant_id": tid, "shares": str(pos.shares),
                      "hwm_per_share": str(_q(pos.hwm_per_share, SHARE_DP, ROUND_HALF_EVEN)),
                      "gain_above_hwm_usd": str(gain), "carry_usd": str(fee)})
    if not lines:
        return None
    return _event(state, "carry_crystallized", at, rate_bps=int(rate_bps),
                  period=period, nav_per_share=str(_q(price, SHARE_DP, ROUND_HALF_EVEN)),
                  carry_total_usd=str(total), lines=lines,
                  receivable=True, funds_moved=False)


# --------------------------------------------- reconciliation against the funding ledger

def normalize_funding_rows(rows: list[dict]) -> list[dict]:
    """Funding-ledger rows -> subscription facts. A bootstrap row is real money
    that simply was not notified at the time, so it counts. A direction=out wrap
    row is evidence rather than a deposit and goes through normalize_wrap_outs."""
    out = []
    for r in rows:
        if r.get("token") not in FUNDING_TOKENS or r.get("direction") == "out":
            continue
        try:
            amount = q_cash(Decimal(str(r.get("amount"))) * STABLE_PAR_USD)
        except (LedgerError, InvalidOperation):
            continue
        if amount <= 0:
            continue
        out.append({"key": r.get("key"), "tenant_id": r.get("tenant_id"),
                    "address": (r.get("address") or "").lower(), "token": r.get("token"),
                    "amount_usd": amount, "timestamp": r.get("timestamp"),
                    "tx": r.get("tx")})
    out.sort(key=lambda d: (str(d.get("timestamp") or ""), str(d.get("key") or "")))
    return out


def normalize_wrap_outs(rows: list[dict]) -> list[dict]:
    """Wrap-outflow evidence rows from the funding ledger: the source token leaving
    the wallet for the collateral token, which is the on-chain proof that the
    venue relayer wrapped a deposit."""
    out = []
    for r in rows:
        if r.get("direction") != "out":
            continue
        try:
            amount = q_cash(Decimal(str(r.get("amount"))) * STABLE_PAR_USD)
        except (LedgerError, InvalidOperation):
            continue
        if amount <= 0:
            continue
        out.append({"key": r.get("key"), "address": (r.get("address") or "").lower(),
                    "token": r.get("token"), "amount_usd": amount,
                    "timestamp": r.get("timestamp"), "tx": r.get("tx")})
    return out


def _epoch(ts) -> float | None:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    if "." in s:
        head, _, tail = s.partition(".")
        off = next((tail[i:] for i, ch in enumerate(tail) if ch in "+-"), "")
        s = head + off
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def pair_wrap_duplicates(deposits: list[dict], wrap_outs: list[dict], *,
                         window_sec: float = WRAP_PAIR_WINDOW_SEC
                         ) -> tuple[list[dict], list[dict]]:
    """Separate the wrapped shadow of a deposit from a genuine second deposit.
    Only on-chain proof counts; no guessing from time windows.

    After the relayer wraps a deposit, one payment can appear as two ledger rows:
    the source token arriving and the collateral arriving. Converting both into
    shares would issue double. But at the same amount inside the same window, a
    wrapped shadow and an independent transfer of the collateral are
    **indistinguishable** in the incoming stream.

    The one thing that separates them: a wrap necessarily has a matching outflow
    of the source token to the collateral contract, recorded as direction=out.

    The test is therefore: fold a collateral arrival if and only if there exists a
    wrap outflow at the same address and amount, inside the window, not already
    consumed by another arrival. With no such evidence it is booked as a real
    deposit. Waiting for evidence beats guessing: the cost of guessing wrong is
    swallowing somebody's money.
    """
    kept, dups = [], []
    unconsumed = list(wrap_outs)
    for dep in deposits:
        evidence = None
        if dep["token"] == "pUSD":
            t = _epoch(dep.get("timestamp"))
            for wo in unconsumed:
                if wo["address"] != dep["address"] or wo["amount_usd"] != dep["amount_usd"]:
                    continue
                tw = _epoch(wo.get("timestamp"))
                if t is not None and tw is not None and abs(t - tw) > window_sec:
                    continue
                evidence = wo
                break
        if evidence is not None:
            unconsumed.remove(evidence)
            dups.append({**dep, "wrap_evidence_key": evidence.get("key"),
                         "wrap_evidence_tx": evidence.get("tx")})
        else:
            kept.append(dep)
    return kept, dups


def reconcile_funding(funding_rows: list[dict], events: list[dict], *,
                      tenant_filter: str | None = None) -> dict:
    """Reconcile the funding ledger against the share ledger.

    Both directions of error matter, at different severities. A deposit with no
    shares issued means somebody's money is not recorded against them. Shares with
    no deposit behind them means shares were issued out of nothing, diluting
    everyone — the more serious of the two, so it is reported separately.
    """
    deposits, wrap_dups = pair_wrap_duplicates(normalize_funding_rows(funding_rows),
                                               normalize_wrap_outs(funding_rows))
    if tenant_filter:
        deposits = [d for d in deposits if d["tenant_id"] == tenant_filter]
    subs = [e for e in events if e.get("kind") == "subscribe"]
    if tenant_filter:
        subs = [e for e in subs if e.get("tenant_id") == tenant_filter]
    by_key = {}
    for e in subs:
        k = (e.get("source") or {}).get("key")
        if k:
            by_key.setdefault(k, []).append(e)

    matched, mismatched, missing = [], [], []
    for dep in deposits:
        hits = by_key.pop(dep["key"], None)
        if not hits:
            missing.append({"key": dep["key"], "tenant_id": dep["tenant_id"],
                            "amount_usd": str(dep["amount_usd"]), "tx": dep.get("tx")})
            continue
        booked = sum((Decimal(h["cash_usd"]) for h in hits), Decimal(0))
        row = {"key": dep["key"], "tenant_id": dep["tenant_id"],
               "deposit_usd": str(dep["amount_usd"]), "booked_usd": str(booked)}
        (matched if booked == dep["amount_usd"] else mismatched).append(row)
    orphans = [{"key": k, "tenant_id": e[0].get("tenant_id"), "booked_usd": e[0].get("cash_usd")}
               for k, e in by_key.items()]
    unsourced = [e for e in subs if not (e.get("source") or {}).get("key")]

    report = {
        "ok": not (mismatched or missing or orphans),
        "deposits": len(deposits), "wrap_duplicates_dropped": len(wrap_dups),
        "subscriptions": len(subs), "unsourced_subscriptions": len(unsourced),
        "matched": matched, "amount_mismatch": mismatched,
        "missing_subscription": missing, "orphan_subscription": orphans,
        "deposit_total_usd": str(sum((d["amount_usd"] for d in deposits), Decimal(0))),
        "booked_total_usd": str(sum((Decimal(e["cash_usd"]) for e in subs), Decimal(0))),
    }
    if not subs and deposits:
        report["note"] = ("this pool has not been opened (share accounting is not "
                          "enabled) - expected under a segregated-account structure, "
                          "and not a missing record")
    return report


# --------------------------- venue activity stream -> NAV valuation points (adapter)

def polymarket_nav_marks(activity: list[dict], *, opening_cash_usd) -> list[dict]:
    """Account activity stream -> a (ts, nav_usd) valuation series. A read-only
    adapter: it places no order and signs nothing.

    Positions are carried at cost, so buying and selling do not move NAV by
    themselves; NAV changes only with **realised** PnL, the difference between a
    sale or redemption and average cost. Without book snapshots this is the
    defensible basis, and once positions are flat NAV equals cash exactly, so the
    terminal value reconciles to the real account balance to the cent. The cash
    field is the actual cash movement including fees, not notional.
    """
    cash = q_cash(opening_cash_usd)
    inventory: dict[tuple, list[Decimal]] = {}         # (conditionId, outcomeIndex) -> [shares, cost]
    marks = []
    for row in sorted(activity, key=lambda r: (int(r.get("timestamp") or 0),
                                               str(r.get("transactionHash") or ""))):
        kind, side = row.get("type"), row.get("side")
        usdc = q_cash(row.get("usdcSize") or 0)
        size = Decimal(str(row.get("size") or 0))
        cond = row.get("conditionId")
        key = (cond, row.get("outcomeIndex"))
        if kind == "TRADE" and side == "BUY":
            cash -= usdc
            lot = inventory.setdefault(key, [Decimal(0), Decimal(0)])
            lot[0] += size
            lot[1] += usdc
        elif kind == "TRADE" and side == "SELL":
            cash += usdc
            lot = inventory.setdefault(key, [Decimal(0), Decimal(0)])
            cost_out = q_cash(CTX.divide(lot[1] * size, lot[0])) if lot[0] > 0 else Decimal(0)
            lot[0] -= size
            lot[1] -= cost_out
        elif kind == "REDEEM":
            # Settlement applies to the **whole condition**. A redemption row
            # reports the winning outcome index, which is not necessarily the one
            # held, so matching on that index would leave the losing leg in
            # inventory forever and inflate NAV.
            cash += usdc
            for k in [k for k in inventory if k[0] == cond]:
                inventory[k] = [Decimal(0), Decimal(0)]
        else:
            continue
        held_cost = sum((v[1] for v in inventory.values()), Decimal(0))
        marks.append({"ts": row.get("timestamp"), "nav_usd": q_cash(cash + held_cost),
                      "cash_usd": cash, "held_cost_usd": held_cost,
                      "title": row.get("title")})
    return marks


# ---------------------------------------------------------------------- persistence

def pool_dir(pool_id: str) -> str:
    if not _POOL_ID_RE.match(pool_id or ""):
        raise LedgerError(f"unsafe pool_id {pool_id!r}")
    return os.path.join(SHARES_ROOT, pool_id)


def events_file(pool_id: str) -> str:
    return os.path.join(pool_dir(pool_id), "events.jsonl")


def append_event(pool_id: str, event: dict) -> dict:
    gstore.append_jsonl(events_file(pool_id), event)
    return event


def load_events(pool_id: str) -> list[dict]:
    path = events_file(pool_id)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_state(pool_id: str) -> PoolState | None:
    events = load_events(pool_id)
    return replay(events) if events else None


# ------------------------------------------------------------------------- selftest

def selftest() -> dict[str, bool]:
    checks: dict[str, bool] = {}

    # --- boundary: this module must not pull in the execution stack -------------
    # The import surface is an attack surface. Scan only the production section:
    # the assertion itself names those tokens, so scanning the whole file would
    # match itself.
    prod_src = open(os.path.abspath(__file__), encoding="utf-8").read().split("\ndef selftest(")[0]
    checks["no_execution_imports"] = not re.search(
        r"^\s*(import|from)\s+(executor|arm|order|wallet|onboarding|polymarket_execution)\b",
        prod_src, re.M)
    checks["no_money_moving_tokens"] = not re.search(
        r"\b(private_key|sign_order|transfer_erc20|post_order|execute_order)\b", prod_src)

    # --- basics: the first subscription starts at 1.0 --------------------------
    pool = "st_pool"
    st = replay([open_pool(pool, mode="pooled", at="2026-01-01T00:00:00Z")])
    ev = subscribe(st, "A", "100", at="2026-01-01T00:00:01Z", source={"ledger": "t", "key": "d1"})
    apply(st, ev)
    checks["first_subscription_prices_at_one"] = (
        Decimal(ev["shares_issued"]) == Decimal("100") and st.nav_per_share == Decimal(1))
    checks["equity_equals_cash_before_pnl"] = st.equity_usd("A") == Decimal("100.000000")

    # --- a return issues no shares; it raises value per share ------------------
    mk, dist = mark(st, "110", at="2026-01-02T00:00:00Z")
    apply(st, mk)
    apply(st, dist)
    checks["pnl_does_not_mint_shares"] = st.shares_outstanding == Decimal("100")
    checks["pnl_lifts_nav_per_share"] = st.nav_per_share == Decimal("1.1")
    checks["distribution_sums_to_nav_delta"] = (
        sum((Decimal(a["pnl_usd"]) for a in dist["allocations"]), Decimal(0))
        == Decimal(dist["pnl_usd"]) == Decimal("10.000000"))

    # --- a later subscriber gains nothing at an earlier one's expense ----------
    before = st.equity_usd("A")
    ev2 = subscribe(st, "B", "55", at="2026-01-02T00:01:00Z", source={"ledger": "t", "key": "d2"})
    apply(st, ev2)
    checks["late_subscriber_gets_fair_shares"] = Decimal(ev2["shares_issued"]) == Decimal("50")
    checks["subscription_does_not_dilute_incumbent"] = st.equity_usd("A") == before
    checks["subscription_leaves_price_unchanged"] = st.nav_per_share == Decimal("1.1")

    # --- subsequent PnL splits pro rata and sums exactly to delta(NAV) ---------
    mk2, dist2 = mark(st, "148.5", at="2026-01-03T00:00:00Z")     # -10% on 165
    apply(st, mk2)
    apply(st, dist2)
    alloc = {a["tenant_id"]: Decimal(a["pnl_usd"]) for a in dist2["allocations"]}
    checks["loss_split_pro_rata"] = (alloc["A"] == Decimal("-11.000000")
                                     and alloc["B"] == Decimal("-5.500000"))
    checks["allocations_sum_exactly"] = sum(alloc.values()) == Decimal(dist2["pnl_usd"])
    checks["equity_sums_to_nav"] = st.residual_usd() == Decimal(0)

    # --- redemption takes only that holder's share and touches nobody else -----
    b_before = st.equity_usd("B")
    rd = redeem(st, "A", cash_usd="49.5", at="2026-01-03T01:00:00Z",
                source={"ledger": "t", "key": "w1"})
    apply(st, rd)
    checks["redeem_burns_priced_shares"] = Decimal(rd["shares_burned"]) == Decimal("50")
    checks["redeem_leaves_others_untouched"] = st.equity_usd("B") == b_before
    checks["redeem_leaves_price_unchanged"] = st.nav_per_share == Decimal("0.99")
    try:
        redeem(st, "A", shares="9999", at="2026-01-03T02:00:00Z")
        checks["over_redemption_refused"] = False
    except LedgerError:
        checks["over_redemption_refused"] = True

    # --- idempotence, ordering, isolation --------------------------------------
    try:
        apply(st, subscribe(st, "A", "10", at="2026-01-04T00:00:00Z",
                            source={"ledger": "t", "key": "d1"}))
        checks["duplicate_source_refused"] = False
    except LedgerError:
        checks["duplicate_source_refused"] = True
    try:
        subscribe(st, "A", "10", at="2025-12-01T00:00:00Z", source={"ledger": "t", "key": "d9"})
        checks["backdated_event_refused"] = False
    except LedgerError:
        checks["backdated_event_refused"] = True

    seg = replay([open_pool("seg_x", mode="segregated", at="2026-01-01T00:00:00Z")])
    apply(seg, subscribe(seg, "solo", "10", at="2026-01-01T00:00:01Z"))
    try:
        subscribe(seg, "intruder", "10", at="2026-01-01T00:00:02Z")
        checks["segregated_pool_refuses_second_tenant"] = False
    except LedgerError:
        checks["segregated_pool_refuses_second_tenant"] = True

    # --- rounding stress: a non-terminating value per share must not erode the
    #     pool ------------------------------------------------------------------
    stress = replay([open_pool("st_round", mode="pooled", at="2026-01-01T00:00:00Z")])
    residuals = []
    for i, (tid, cash) in enumerate([("A", "33.333333"), ("B", "0.7"), ("C", "1234.56")]):
        apply(stress, subscribe(stress, tid, cash, at=f"2026-02-0{i + 1}T00:00:00Z"))
        apply(stress, mark(stress, str(Decimal(stress.nav_usd) * Decimal("1.0000007") + 7),
                           at=f"2026-02-0{i + 1}T12:00:00Z")[0])
        residuals.append(stress.residual_usd())
    apply(stress, redeem(stress, "B", cash_usd="0.333333", at="2026-02-05T00:00:00Z"))
    residuals.append(stress.residual_usd())
    checks["rounding_never_leaves_pool_short"] = all(r >= 0 for r in residuals)
    checks["rounding_residual_stays_dust"] = all(r < Decimal("0.000010") for r in residuals)

    # --- a full redemption resets the starting value ---------------------------
    empty = replay([open_pool("st_empty", mode="pooled", at="2026-01-01T00:00:00Z")])
    apply(empty, subscribe(empty, "A", "10", at="2026-01-01T00:00:01Z"))
    apply(empty, mark(empty, "12", at="2026-01-02T00:00:00Z")[0])
    apply(empty, redeem(empty, "A", shares="10", at="2026-01-03T00:00:00Z"))
    checks["full_redemption_empties_pool"] = (empty.shares_outstanding == 0
                                              and empty.nav_usd == Decimal(0))
    ev3 = subscribe(empty, "C", "20", at="2026-01-04T00:00:00Z")
    checks["rebases_at_one_after_empty"] = Decimal(ev3["nav_per_share"]) == Decimal(1)

    # --- the performance fee is computed, never collected here -----------------
    prev = carry_preview(st, rate_bps=1000)
    checks["carry_preview_does_not_move_funds"] = (
        prev["accrued_only"] is True and st.nav_usd == Decimal("99.000000"))
    checks["carry_zero_below_hwm"] = Decimal(prev["carry_total_usd"]) == Decimal(0)
    up = replay([open_pool("st_c", mode="pooled", at="2026-01-01T00:00:00Z")])
    apply(up, subscribe(up, "A", "100", at="2026-01-01T00:00:01Z"))
    apply(up, mark(up, "150", at="2026-01-02T00:00:00Z")[0])
    checks["carry_computed_on_gain_above_hwm"] = (
        Decimal(carry_preview(up, rate_bps=1000)["carry_total_usd"]) == Decimal("5.000000"))
    checks["carry_default_rate_is_ten_percent"] = CARRY_RATE_BPS == 1000

    # --- crystallisation issues a receivable and ratchets the mark; it moves no
    #     money, which is the core promise --------------------------------------
    cz = replay([open_pool("st_cz", mode="segregated", at="2026-01-01T00:00:00Z")])
    apply(cz, subscribe(cz, "U", "1000", at="2026-01-01T00:00:01Z"))
    apply(cz, mark(cz, "1200", at="2026-02-01T00:00:00Z")[0])       # +200 profit
    ev1 = crystallize_carry(cz, at="2026-02-01T00:01:00Z", period="2026-01")
    nav_before, shares_before = cz.nav_usd, cz.shares_outstanding
    apply(cz, ev1)
    checks["crystallize_bills_ten_percent_of_gain"] = (
        Decimal(ev1["carry_total_usd"]) == Decimal("20.000000"))
    checks["crystallize_moves_no_funds"] = (
        cz.nav_usd == nav_before and cz.shares_outstanding == shares_before
        and ev1["funds_moved"] is False and ev1["receivable"] is True)
    checks["crystallize_ratchets_hwm"] = cz.tenants["U"].hwm_per_share == Decimal("1.2")
    checks["crystallize_user_equity_untouched"] = cz.equity_usd("U") == Decimal("1200.000000")

    # the same gain is never charged twice
    checks["crystallize_twice_same_nav_is_free"] = (
        crystallize_carry(cz, at="2026-02-01T00:02:00Z") is None)
    # no fee during a loss
    apply(cz, mark(cz, "900", at="2026-03-01T00:00:00Z")[0])
    checks["crystallize_zero_while_underwater"] = (
        crystallize_carry(cz, at="2026-03-01T00:01:00Z") is None)
    # still no fee while recovering: getting back to the old high restores the
    # mark rather than creating new profit
    apply(cz, mark(cz, "1200", at="2026-04-01T00:00:00Z")[0])
    checks["crystallize_zero_recovering_to_hwm"] = (
        crystallize_carry(cz, at="2026-04-01T00:01:00Z") is None)
    # above the old high-water mark, only the **excess** is charged, not the whole
    # recovery
    apply(cz, mark(cz, "1300", at="2026-05-01T00:00:00Z")[0])
    ev2 = crystallize_carry(cz, at="2026-05-01T00:01:00Z", period="2026-04")
    apply(cz, ev2)
    checks["crystallize_bills_only_new_high"] = (
        Decimal(ev2["carry_total_usd"]) == Decimal("10.000000"))
    checks["carry_accrued_accumulates"] = cz.carry_accrued_usd == Decimal("30.000000")
    # a subscription is not profit: it raises NAV and accrues no fee
    apply(cz, subscribe(cz, "U", "500", at="2026-05-02T00:00:00Z"))
    checks["deposit_never_billed_as_profit"] = (
        crystallize_carry(cz, at="2026-05-02T00:01:00Z") is None)
    checks["carry_state_untouched_by_preview"] = up.nav_usd == Decimal("150.000000")

    # --- reconciliation: a wrapped shadow folds only on on-chain evidence, never
    #     on a time-window guess -------------------------------------------------
    rows = [
        {"key": "k1", "tenant_id": "t1", "address": "0xAA", "token": "USDC.e",
         "amount": 24.19, "timestamp": "2026-06-18T16:04:32Z", "tx": "0xa"},
        # wrap proof: an equal amount of the source token leaving for the
        # collateral contract, recorded as direction=out
        {"key": "w1", "tenant_id": "t1", "address": "0xAA", "token": "USDC.e",
         "amount": 24.19, "timestamp": "2026-06-18T16:04:46Z", "tx": "0xb",
         "direction": "out"},
        {"key": "k2", "tenant_id": "t1", "address": "0xaa", "token": "pUSD",
         "amount": 24.19, "timestamp": "2026-06-18T16:04:46Z", "tx": "0xb"},
        {"key": "k3", "tenant_id": "t1", "address": "0xAA", "token": "USDC",
         "amount": 5.0, "timestamp": "2026-06-20T00:00:00Z", "tx": "0xc"},
    ]
    kept, dups = pair_wrap_duplicates(normalize_funding_rows(rows), normalize_wrap_outs(rows))
    checks["wrap_twin_collapses_only_with_onchain_evidence"] = (
        len(kept) == 2 and len(dups) == 1 and dups[0]["wrap_evidence_key"] == "w1")
    # An independent collateral transfer at the same amount in the same window,
    # with no outflow evidence, must be booked. Guessing it is a shadow would
    # swallow it.
    indep = [
        {"key": "k4", "tenant_id": "t1", "address": "0xAA", "token": "USDC",
         "amount": 50.0, "timestamp": "2026-07-01T10:00:00Z", "tx": "0xd"},
        {"key": "k5", "tenant_id": "t1", "address": "0xAA", "token": "pUSD",
         "amount": 50.0, "timestamp": "2026-07-01T10:30:00Z", "tx": "0xe"},
    ]
    kept_i, dups_i = pair_wrap_duplicates(normalize_funding_rows(indep),
                                          normalize_wrap_outs(indep))
    checks["independent_pusd_in_window_not_swallowed"] = (
        len(kept_i) == 2 and not dups_i)
    # one piece of outflow evidence is consumed once: two equal arrivals cannot
    # both fold against the same wrap
    twice = rows + [{"key": "k6", "tenant_id": "t1", "address": "0xAA", "token": "pUSD",
                     "amount": 24.19, "timestamp": "2026-06-18T16:05:00Z", "tx": "0xf"}]
    kept_t, dups_t = pair_wrap_duplicates(normalize_funding_rows(twice),
                                          normalize_wrap_outs(twice))
    checks["wrap_evidence_consumed_once"] = len(dups_t) == 1 and len(kept_t) == 3
    # an outflow evidence row is never itself converted into a deposit
    checks["wrap_out_row_never_counted_as_deposit"] = all(
        d["key"] != "w1" for d in normalize_funding_rows(rows))

    rec_pool = replay([open_pool("st_rec", mode="segregated", at="2026-06-18T00:00:00Z")])
    rec_events = [open_pool("st_rec", mode="segregated", at="2026-06-18T00:00:00Z")]
    for dep in kept:
        e = subscribe(rec_pool, "t1", dep["amount_usd"], at=dep["timestamp"],
                      source={"ledger": "funding", "key": dep["key"]})
        rec_events.append(e)
        apply(rec_pool, e)
    rep = reconcile_funding(rows, rec_events)
    checks["reconcile_clean_when_every_deposit_booked"] = (
        rep["ok"] and rep["deposits"] == 2 and rep["wrap_duplicates_dropped"] == 1)
    checks["reconcile_flags_missing_subscription"] = (
        reconcile_funding(rows, rec_events[:2])["missing_subscription"][0]["key"] == "k3")
    ghost = rec_events + [dict(rec_events[-1], source={"ledger": "funding", "key": "nope"})]
    checks["reconcile_flags_orphan_subscription"] = (
        reconcile_funding(rows, ghost)["orphan_subscription"][0]["key"] == "nope")

    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Unitized share ledger: pure accounting, no money operation")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--status", metavar="POOL_ID", help="print a pool's current share state")
    ap.add_argument("--reconcile", metavar="POOL_ID", help="reconcile against the funding ledger")
    a = ap.parse_args(argv)

    if a.selftest:
        checks = selftest()
        for name, ok in checks.items():
            print(("  PASS  " if ok else "  FAIL  ") + name)
        bad = [k for k, v in checks.items() if not v]
        print(f"selftest: {len(checks) - len(bad)}/{len(checks)} " +
              ("ALL PASS" if not bad else f"FAIL {bad}"))
        return 0 if not bad else 1

    if a.status:
        state = load_state(a.status)
        if state is None:
            print(f"no ledger for pool {a.status}")
            return 1
        print(json.dumps(state.summary(), ensure_ascii=False, indent=1))
        return 0

    if a.reconcile:
        state_events = load_events(a.reconcile)
        from marketflow.guardian import funding_watcher as fw
        rows = []
        if os.path.exists(fw.LEDGER):
            with open(fw.LEDGER, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f if x.strip()]
        print(json.dumps(reconcile_funding(rows, state_events), ensure_ascii=False, indent=1))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
