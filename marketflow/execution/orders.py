#!/usr/bin/env python3
"""Polymarket auto-execution module v0.1 (dry_run by default, fuse-gated).

This is the execution leg of the Polymarket auto-trade chain. It can construct
real CLOB orders through the official polymarket-client SDK (EIP-712 signing and
L2 auth handled by the SDK), submit and cancel them, and read fills back. Every
path is wrapped by code-enforced fuses so a live order can only ever fire when
multiple independent brakes are released at once.

Exit-first by design: the sell / close path is the primary, fully-built path
(closing risk must always be possible); buying / opening exposure is the gated,
hard-capped secondary path.

Fuses (all code-enforced, fail-closed):
  1. capital caps: cumulative live BUY deploy <= total cap and any
     single order notional <= per-trade cap; a BUY that would breach
     either is refused. SELL (exit) is never blocked by the deploy caps, only
     bounded by held shares (no naked short).
  2. kill switch: a flag file or env var; when set, all live execution refuses.
  3. dry_run is the default. Live requires a valid armed arm-state file — the
     single control file an operator writes, carrying armed + mode + phrase and
     an optional expiry and budget. The arm mode must also permit the order side.
     No valid armed arm-state means a forced dry run.
  4. exit-first: SELL/close is the primary path; BUY/open is secondary + capped.

Boundaries:
  - dry_run never signs, submits, cancels, or moves funds; it only records the
    order it WOULD place plus a read-only fill estimate;
  - secrets are read only from local secret refs, never printed, never ledgered;
    every ledger row passes assert_no_secret_leak;
  - the venue SDK derives the signature scheme from wallet=funder, so there is no
    signature_type integer to pass; wallet_type is detected and asserted instead.

Run the dry_run selftest (no network, no SDK, no secrets):
  python3 -m marketflow.execution.orders --selftest

Live execution is an operator decision and is never exercised by this module's
self-test or by an unattended run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from hashlib import sha256
from typing import Any

try:
    from marketflow.execution.sdk_proxy import (
        polymarket_sdk_proxy_url,
        proxied_secure_client_transports,
    )
except ImportError:  # pragma: no cover - package import fallback.
    from marketflow.execution.sdk_proxy import (
        polymarket_sdk_proxy_url,
        proxied_secure_client_transports,
    )


from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
OUT_DIR = runtime_path("execution", "orders")
DEFAULT_LEDGER = os.path.join(OUT_DIR, "execution_ledger.jsonl")
DEFAULT_LATEST_JSON = os.path.join(OUT_DIR, "latest.json")
DEFAULT_LATEST_SUMMARY = os.path.join(OUT_DIR, "latest_summary.md")
DEFAULT_SELFTEST = os.path.join(OUT_DIR, "selftest_report.json")
DEFAULT_KILL_FILE = os.path.join(OUT_DIR, "EXECUTION_KILL")
DEFAULT_GLOBAL_HALT_FILE = runtime_path("execution", "HALT")
DEFAULT_SECRET_DIR = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets")
# Single-source arm state: one control file that both this module and the daemon
# read, written only by an explicit operator action. It lives under the runtime
# tree rather than beside secrets because it carries no credential — only the
# armed flag, the mode and a marker.
DEFAULT_ARM_STATE_FILE = runtime_path("execution", "polymarket_arm_state.json")

SCHEMA_VERSION = "polymarket-execution-v0.1"
ARM_STATE_SCHEMA_VERSION = "polymarket-arm-state-v1"
LIVE_PENDING_MODE = "LIVE_PENDING"
LIVE_EXECUTED_MODE = "LIVE_EXECUTED"

# --- Capital scale -----------------------------------------------------------
# Every cap and ceiling below is a FRACTION of the book this deployment declares,
# so the same code governs a $250k book and a $500M book without an edit. Set the
# book size once, in the environment, and the whole fuse surface moves with it.
#
#     MARKETFLOW_CAPITAL_BASE_USD=250000000
#
# The fallback is a reference book, not a recommendation: a deployment that never
# sets the variable is sized against it, which is exactly why live execution is
# additionally gated on an arm-state written by a separate process carrying its
# own caps (see load_arm_state). Module defaults never authorise money on their
# own; they bound what an arm-state is allowed to ask for.
REFERENCE_CAPITAL_USD = float(os.environ.get("MARKETFLOW_CAPITAL_BASE_USD") or 1_000_000.0)

# Operating defaults, as fractions of the declared book.
DEFAULT_TOTAL_DEPLOY_FRACTION = 0.20      # simultaneously deployed
DEFAULT_PER_TRADE_FRACTION = 0.005        # single order
DEFAULT_DRAWDOWN_FRACTION = 0.02          # realised loss per budget epoch
# Fat-finger ceilings: the most an arm-state may raise a cap to. These block a
# stray extra zero; they are not the risk policy, which is the cap itself.
CEILING_TOTAL_FRACTION = 1.00
CEILING_PER_TRADE_FRACTION = 0.05
CEILING_DRAWDOWN_FRACTION = 0.10

DEFAULT_MAX_TOTAL_DEPLOY_USD = REFERENCE_CAPITAL_USD * DEFAULT_TOTAL_DEPLOY_FRACTION
DEFAULT_MAX_PER_TRADE_USD = REFERENCE_CAPITAL_USD * DEFAULT_PER_TRADE_FRACTION
# Hard loss bound under net-exposure budgeting: once realised net loss inside a
# budget epoch (buys - sells - redemptions - live mark, rebuilt from the account's
# own flows) reaches this, BUY halts and exits stay open.
DEFAULT_MAX_DRAWDOWN_USD = REFERENCE_CAPITAL_USD * DEFAULT_DRAWDOWN_FRACTION
# Principal-account fat-finger ceilings. The principal's own caps may be raised
# up to these by an arm-state write. Delegated mandates NEVER use this path: they
# stay bound by their own tier ceilings in marketflow/guardian/executor.py, and a mandate
# may only tighten, never widen, what it was granted.
OWNER_CAP_CEILING_TOTAL_USD = REFERENCE_CAPITAL_USD * CEILING_TOTAL_FRACTION
OWNER_CAP_CEILING_PER_TRADE_USD = REFERENCE_CAPITAL_USD * CEILING_PER_TRADE_FRACTION
OWNER_CAP_CEILING_DRAWDOWN_USD = REFERENCE_CAPITAL_USD * CEILING_DRAWDOWN_FRACTION

# How long a principal's authorisation stands before it must be reconfirmed.
# Expiry does not stop everything; it downgrades to exit_only, and load_arm_state
# explains why. marketflow/guardian/arm.py carries the same interval for the same reason
# (automation stopping silently is worse than being asked to reconfirm) but
# deliberately does NOT share this constant: the two paths have their own
# cadences, they may diverge, and coupling them would let one drag the other.
OWNER_ARM_TTL_DAYS = 30

KILL_SWITCH_ENV = "MARKETFLOW_POLYMARKET_KILL"
LIVE_ACK_PHRASE = "OWNER_APPROVES_POLYMARKET_LIVE_EXECUTION"
# "entry_allowlisted" is the delegated-mandate entry mode: it permits the same sides
# as "full", but names the capability the user actually authorised instead of
# granting a blanket one. Which SIGNAL SOURCE may open a position is enforced by
# the guardian executor's source allowlist — the order module never sees a source — so this mode
# is a defence-in-depth marker plus an audit trail of what the user agreed to, not
# the source restriction itself. Owner-path arm writes (bridge) never emit it.
ARM_MODES = ("off", "exit_only", "entry_allowlisted", "full")
LIVE_VALID_ARM_MODES = ("exit_only", "entry_allowlisted", "full")
# The only writer whose arm file this module accepts: an explicit operator action.
# A delegated mandate's arm file carries a different writer and can never arm this
# path, and vice versa (see the guardian executor's GUARDIAN_ARM_WRITER).
ARM_WRITER = "operator"
EXPECTED_WALLET_TYPE = "DEPOSIT_WALLET"
WALLET_TYPES = ("EOA", "POLY_PROXY", "GNOSIS_SAFE", "DEPOSIT_WALLET")

# Limit price guardrail: prediction-market binary prices live in (0,1). This is a
# well-formedness bound on the price field, NOT the longshot policy — that is the
# BUY price floor below, which binds on market orders too.
MIN_LIMIT_PRICE = 0.01
MAX_LIMIT_PRICE = 0.99

# BUY price floor. Every entry path — daemon, Guardian user
# command, CLI intent_from_config — passes through evaluate_fuses, so this is the
# single place the floor covers all of them. It binds on the limit price for limit
# orders and on max_price for market orders (the daemon's market-quality gate only
# sees the daemon's own path, and Guardian buys are market FOK).
# Accounting basis: px<0.10 is 1.36% of deployed capital and loses more than the
# whole sample's net loss; excluding it leaves the other 98.6% break-even after
# fees (+0.107%). A gate, not a ramp — px<0.02 shows no such bias.
# The capability is not removed: OrderIntent.price_floor_override_reason admits a
# named exception, and the reason lands in the ledger.
BUY_PRICE_FLOOR = 0.10
# Money surface. Turning this on adds no incremental refusals, and that is an
# identity rather than a property of one sample: the taker fee is bounded above by
# rate/4, which is well inside the edge buffer an entry must already clear, and
# the longshot band is already stricter than this floor. The value is in two
# places. It welds the identity into the order layer so configuration drift cannot
# quietly widen it, and it is the one common entry point for paths that bypass the
# daemon's own gates (a delegated BUY placed as a market FOK, for instance).
# Explicit exceptions still go through price_floor_override_reason and land in the
# ledger.
BUY_PRICE_FLOOR_ENFORCE = True

SECRET_REFS = {
    "api_key": "polymarket_api_key.txt",
    "api_secret": "polymarket_api_secret.txt",
    "passphrase": "polymarket_passphrase.txt",
    "funder_address": "polymarket_funder_address.txt",
}
RAW_PRIVATE_KEY_REF = "polymarket_private_key.txt"

EXECUTION_BOUNDARIES = [
    "dry_run_default",
    "live_requires_armed_arm_state",
    "live_mode_must_permit_side",
    "kill_switch_fail_closed",
    "global_halt_buy_only",
    "explicit_budget_epoch_required_for_live",
    "maker_first_buy_entry",
    "taker_buy_only_for_in_play_speed_window",
    "idempotency_key_no_duplicate",
    "add_to_existing_position_allowed_with_caps",
    "live_budget_rebuilt_from_account_fills",
    "capital_caps_enforced",
    "exit_first_sell_primary",
    "no_naked_short",
    "no_secret_print",
    "no_secret_in_ledger",
    "wallet_address_masked_in_ledger",
    "scoped_polymarket_sdk_http_proxy",
]

# Only these SDK methods are ever referenced on a call path. create_*/post_order/
# cancel_* are LIVE-only and reached solely through execute/cancel after the fuse
# gate passes. place_limit_order / place_market_order (one-shot create+post) are
# deliberately NOT used so dry_run can never accidentally submit.
ALLOWED_SDK_BUILD_METHODS = ("create_limit_order", "create_market_order")
ALLOWED_SDK_SUBMIT_METHODS = ("post_order",)
ALLOWED_SDK_CANCEL_METHODS = ("cancel_order", "cancel_orders", "cancel_all", "cancel_market_orders")
ALLOWED_SDK_READBACK_METHODS = ("get_order", "list_account_trades", "list_positions", "list_open_orders")
ALLOWED_SDK_QUOTE_METHODS = ("get_order_book", "get_price", "estimate_market_price", "get_midpoint")

FORBIDDEN_SDK_METHODS = (
    "place_limit_order",
    "place_market_order",
    "redeem_positions",
    "merge_positions",
    "split_position",
    "transfer_erc20",
    "approve_erc20",
    "approve_erc1155_for_all",
    "setup_trading_approvals",
    "setup_gasless_wallet",
)


class ExecutionError(Exception):
    """Raised for fail-loud execution errors."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_obj(obj: Any) -> str:
    return sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def write_json(path: str, data: Any) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def append_jsonl(path: str, row: dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(canonical_json(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        out = float(value)
        return out if math.isfinite(out) else None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def rounded_float(value: Any) -> float | None:
    f = to_float(value)
    return round(f, 8) if f is not None else None


def mask_address(value: Any) -> str | None:
    """Mask an EVM-style address; keep a short head/tail for recognizability."""
    text = safe_str(value)
    if text is None:
        return None
    body = text[2:] if text.lower().startswith("0x") else text
    if len(body) <= 8:
        return "0x" + "*" * len(body)
    return f"0x{body[:4]}…{body[-4:]}"


def attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def read_secret_ref(secret_dir: str, filename: str) -> str:
    path = os.path.join(secret_dir, filename)
    with open(path, encoding="utf-8") as f:
        value = f.read().strip()
    if not value:
        raise ExecutionError(f"secret ref {filename} is empty")
    return value


def load_polymarket_secret_refs(secret_dir: str) -> dict[str, str]:
    missing = [
        filename
        for filename in SECRET_REFS.values()
        if not os.path.exists(os.path.join(secret_dir, filename))
    ]
    if missing:
        raise ExecutionError(f"missing Polymarket secret refs: {', '.join(sorted(missing))}")
    secrets = {key: read_secret_ref(secret_dir, filename) for key, filename in SECRET_REFS.items()}
    if os.path.exists(os.path.join(secret_dir, OWNER_TURNKEY_GATE_FILE)):
        # Validate every enclave ref now. A partial enclave setup must fail
        # before any SDK client is constructed, never fall back to the raw key.
        owner_turnkey_material(secret_dir)
    else:
        raw_key_path = os.path.join(secret_dir, RAW_PRIVATE_KEY_REF)
        if not os.path.exists(raw_key_path):
            raise ExecutionError(f"missing Polymarket secret ref: {RAW_PRIVATE_KEY_REF}")
        secrets["private_key"] = read_secret_ref(secret_dir, RAW_PRIVATE_KEY_REF)
    return secrets


def secret_ref_summary(secret_dir: str) -> dict[str, Any]:
    refs = list(SECRET_REFS.values())
    if not os.path.exists(os.path.join(secret_dir, OWNER_TURNKEY_GATE_FILE)):
        refs.append(RAW_PRIVATE_KEY_REF)
    return {
        "secret_dir": secret_dir,
        "refs_present": sorted(refs),
        "secrets_redacted": True,
    }


@dataclass
class OrderIntent:
    """A normalized order request, independent of dry_run/live and of the SDK."""

    action: str  # EXIT or ENTER
    side: str  # BUY or SELL
    token_id: str
    order_kind: str  # limit or market
    market_id: str | None = None
    market_slug: str | None = None
    outcome: str | None = None
    price: float | None = None  # limit price in (0,1)
    size: float | None = None  # shares (limit, or market SELL)
    max_spend_usd: float | None = None  # market BUY USD ceiling
    max_price: float | None = None  # market BUY worst price
    min_price: float | None = None  # market SELL worst price
    market_order_type: str = "FOK"  # FAK or FOK for market orders
    post_only: bool = False
    taker_allowed_reason: str | None = None
    expiration: int | None = None
    held_shares: float | None = None  # current held shares of this token (for SELL bound)
    signal_id: str | None = None
    idempotency_key: str | None = None
    settlement_cycle: str | None = None
    note: str | None = None
    price_floor_override_reason: str | None = None  # named exception to BUY_PRICE_FLOOR; lands in the ledger

    def redacted(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "side": self.side,
            "token_id": self.token_id,
            "order_kind": self.order_kind,
            "market_id": self.market_id,
            "market_slug": self.market_slug,
            "outcome": self.outcome,
            "price": self.price,
            "size": self.size,
            "max_spend_usd": self.max_spend_usd,
            "max_price": self.max_price,
            "min_price": self.min_price,
            "market_order_type": self.market_order_type,
            "post_only": self.post_only,
            "taker_allowed_reason": self.taker_allowed_reason,
            "expiration": self.expiration,
            "held_shares": self.held_shares,
            "signal_id": self.signal_id,
            "idempotency_key": self.idempotency_key,
            "settlement_cycle": self.settlement_cycle,
            "note": self.note,
            "price_floor_override_reason": self.price_floor_override_reason,
        }


@dataclass
class FuseCaps:
    max_total_deploy_usd: float = DEFAULT_MAX_TOTAL_DEPLOY_USD
    max_per_trade_usd: float = DEFAULT_MAX_PER_TRADE_USD
    max_drawdown_usd: float = DEFAULT_MAX_DRAWDOWN_USD
    ceiling_total_usd: float = DEFAULT_MAX_TOTAL_DEPLOY_USD
    ceiling_per_trade_usd: float = DEFAULT_MAX_PER_TRADE_USD
    ceiling_drawdown_usd: float = DEFAULT_MAX_DRAWDOWN_USD

    def __post_init__(self) -> None:
        # Caps may only TIGHTEN below the ceiling: any caller-supplied cap (daemon
        # config, CLI flag, arm-state file) is clamped to the ceiling, never above.
        # The DEFAULT ceiling equals the module constants, so any default-built
        # FuseCaps -- delegated mandates, multi-tenant accounts, every caller that
        # does not ask otherwise -- stays bound to the default fractions (per-tenant
        # only tightens). Only the operator path (owner_fuse_caps) raises the
        # ceiling, letting the operator set its own caps up to the fat-finger
        # ceiling. The ceiling itself is clamped to OWNER_CAP_CEILING so a tampered
        # ceiling can't uncap. Single-point structural invariant regardless of who
        # constructs FuseCaps.
        self.ceiling_total_usd = min(float(self.ceiling_total_usd), OWNER_CAP_CEILING_TOTAL_USD)
        self.ceiling_per_trade_usd = min(float(self.ceiling_per_trade_usd), OWNER_CAP_CEILING_PER_TRADE_USD)
        self.ceiling_drawdown_usd = min(float(self.ceiling_drawdown_usd), OWNER_CAP_CEILING_DRAWDOWN_USD)
        self.max_total_deploy_usd = min(float(self.max_total_deploy_usd), self.ceiling_total_usd)
        self.max_per_trade_usd = min(float(self.max_per_trade_usd), self.ceiling_per_trade_usd)
        self.max_drawdown_usd = min(float(self.max_drawdown_usd), self.ceiling_drawdown_usd)


def owner_fuse_caps(
    max_total_deploy_usd: float | None = None,
    max_per_trade_usd: float | None = None,
    max_drawdown_usd: float | None = None,
) -> FuseCaps:
    """Build caps for the operator's OWN single-account stack, raisable up to the
    operator fat-finger ceiling. Delegated mandates and multi-tenant accounts must
    NOT use this -- they use FuseCaps() directly, which stays bound to the default
    fractions (per-tenant only tightens). None keeps the default value for that
    field.
    """
    return FuseCaps(
        max_total_deploy_usd=(
            DEFAULT_MAX_TOTAL_DEPLOY_USD if max_total_deploy_usd is None else max_total_deploy_usd
        ),
        max_per_trade_usd=(
            DEFAULT_MAX_PER_TRADE_USD if max_per_trade_usd is None else max_per_trade_usd
        ),
        max_drawdown_usd=(
            DEFAULT_MAX_DRAWDOWN_USD if max_drawdown_usd is None else max_drawdown_usd
        ),
        ceiling_total_usd=OWNER_CAP_CEILING_TOTAL_USD,
        ceiling_per_trade_usd=OWNER_CAP_CEILING_PER_TRADE_USD,
        ceiling_drawdown_usd=OWNER_CAP_CEILING_DRAWDOWN_USD,
    )


@dataclass
class FuseResult:
    passed: bool
    forced_dry_run: bool
    kill_switch_active: bool
    arm_valid: bool
    arm_mode: str = "off"
    checks: dict[str, Any] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    deploy_notional_usd: float | None = None
    deployed_before_usd: float | None = None
    remaining_deploy_usd: float | None = None


def normalize_side(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw not in ("BUY", "SELL"):
        raise ExecutionError("order side must be BUY or SELL")
    return raw


def normalize_action(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw not in ("EXIT", "ENTER"):
        raise ExecutionError("order action must be EXIT or ENTER")
    return raw


def normalize_order_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw not in ("limit", "market"):
        raise ExecutionError("order_kind must be limit or market")
    return raw


def build_exit_intent(
    *,
    token_id: str,
    held_shares: float,
    order_kind: str = "market",
    price: float | None = None,
    size: float | None = None,
    market_order_type: str = "FOK",
    min_price: float | None = None,
    market_id: str | None = None,
    market_slug: str | None = None,
    outcome: str | None = None,
    signal_id: str | None = None,
    idempotency_key: str | None = None,
    settlement_cycle: str | None = None,
    note: str | None = None,
) -> OrderIntent:
    """Exit-first primary path: build a SELL intent that closes/reduces a held position."""
    token = safe_str(token_id)
    if not token:
        raise ExecutionError("exit requires a token_id")
    held = to_float(held_shares)
    if held is None or held <= 0:
        raise ExecutionError("exit requires positive held_shares")
    kind = normalize_order_kind(order_kind)
    sell_size = to_float(size) if size is not None else held
    if sell_size is None or sell_size <= 0:
        raise ExecutionError("exit size must be positive")
    if kind == "limit":
        p = to_float(price)
        if p is None:
            raise ExecutionError("limit exit requires a price")
    return OrderIntent(
        action="EXIT",
        side="SELL",
        token_id=token,
        order_kind=kind,
        market_id=safe_str(market_id),
        market_slug=safe_str(market_slug),
        outcome=safe_str(outcome),
        price=to_float(price),
        size=sell_size,
        min_price=to_float(min_price),
        market_order_type=str(market_order_type).strip().upper(),
        held_shares=held,
        signal_id=safe_str(signal_id),
        idempotency_key=safe_str(idempotency_key),
        settlement_cycle=safe_str(settlement_cycle),
        note=note,
    )


def build_entry_intent(
    *,
    token_id: str,
    order_kind: str = "limit",
    price: float | None = None,
    size: float | None = None,
    max_spend_usd: float | None = None,
    max_price: float | None = None,
    market_order_type: str = "FOK",
    post_only: bool = True,
    taker_allowed_reason: str | None = None,
    expiration: int | None = None,
    market_id: str | None = None,
    market_slug: str | None = None,
    outcome: str | None = None,
    signal_id: str | None = None,
    idempotency_key: str | None = None,
    settlement_cycle: str | None = None,
    note: str | None = None,
    price_floor_override_reason: str | None = None,
) -> OrderIntent:
    """Secondary path: build a BUY intent that opens/adds exposure (hard-capped)."""
    token = safe_str(token_id)
    if not token:
        raise ExecutionError("entry requires a token_id")
    kind = normalize_order_kind(order_kind)
    if kind == "limit":
        if to_float(price) is None or to_float(size) is None:
            raise ExecutionError("limit entry requires price and size")
    else:
        if to_float(max_spend_usd) is None:
            raise ExecutionError("market entry requires max_spend_usd so the deploy cap can bound it")
    return OrderIntent(
        action="ENTER",
        side="BUY",
        token_id=token,
        order_kind=kind,
        market_id=safe_str(market_id),
        market_slug=safe_str(market_slug),
        outcome=safe_str(outcome),
        price=to_float(price),
        size=to_float(size),
        max_spend_usd=to_float(max_spend_usd),
        max_price=to_float(max_price),
        market_order_type=str(market_order_type).strip().upper(),
        post_only=bool(post_only),
        taker_allowed_reason=safe_str(taker_allowed_reason),
        expiration=expiration,
        signal_id=safe_str(signal_id),
        idempotency_key=safe_str(idempotency_key),
        settlement_cycle=safe_str(settlement_cycle),
        note=note,
        price_floor_override_reason=safe_str(price_floor_override_reason) or None,
    )


def intent_from_config(config: dict[str, Any]) -> OrderIntent:
    action = normalize_action(config.get("action"))
    if action == "EXIT":
        return build_exit_intent(
            token_id=config.get("token_id"),
            held_shares=config.get("held_shares"),
            order_kind=config.get("order_kind", "market"),
            price=config.get("price"),
            size=config.get("size"),
            market_order_type=config.get("market_order_type", "FOK"),
            min_price=config.get("min_price"),
            market_id=config.get("market_id"),
            market_slug=config.get("market_slug"),
            outcome=config.get("outcome"),
            signal_id=config.get("signal_id"),
            idempotency_key=config.get("idempotency_key"),
            settlement_cycle=config.get("settlement_cycle") or config.get("close_time"),
            note=config.get("note"),
        )
    return build_entry_intent(
        token_id=config.get("token_id"),
        order_kind=config.get("order_kind", "limit"),
        price=config.get("price"),
        size=config.get("size"),
        max_spend_usd=config.get("max_spend_usd"),
        max_price=config.get("max_price"),
        market_order_type=config.get("market_order_type", "FOK"),
        post_only=config.get("post_only", True),
        taker_allowed_reason=config.get("taker_allowed_reason"),
        expiration=config.get("expiration"),
        market_id=config.get("market_id"),
        market_slug=config.get("market_slug"),
        outcome=config.get("outcome"),
        signal_id=config.get("signal_id"),
        idempotency_key=config.get("idempotency_key"),
        settlement_cycle=config.get("settlement_cycle") or config.get("close_time"),
        note=config.get("note"),
        price_floor_override_reason=config.get("price_floor_override_reason"),
    )


def deploy_notional_usd(intent: OrderIntent) -> float | None:
    """Worst-case USD that leaves the wallet for this intent.

    BUY is capital deployed (gated). SELL is an exit (returns cash, never gated by
    deploy caps), so it has no deploy notional.
    """
    if intent.side != "BUY":
        return None
    if intent.order_kind == "limit":
        price = to_float(intent.price)
        size = to_float(intent.size)
        if price is None or size is None:
            return None
        return round(price * size, 8)
    spend = to_float(intent.max_spend_usd)
    return round(spend, 8) if spend is not None else None


def kill_switch_state(kill_file: str) -> dict[str, Any]:
    env_set = os.environ.get(KILL_SWITCH_ENV, "").strip() not in ("", "0", "false", "False")
    file_set = os.path.exists(kill_file)
    halt_set = os.path.exists(DEFAULT_GLOBAL_HALT_FILE)
    return {
        "active": bool(env_set or file_set),
        "env_var": KILL_SWITCH_ENV,
        "env_set": env_set,
        "kill_file": kill_file,
        "file_set": file_set,
        "global_halt_file": DEFAULT_GLOBAL_HALT_FILE,
        "global_halt_set": halt_set,
        "buy_halt_active": bool(env_set or file_set or halt_set),
    }


def arm_mode_permits(mode: str, side: str) -> bool:
    """Which order sides a given arm mode permits.
    off=>none, exit_only=>SELL, entry_allowlisted=>both (delegated-mandate entry), full=>both."""
    if mode in ("full", "entry_allowlisted"):
        return True
    if mode == "exit_only":
        return side == "SELL"
    return False


def load_arm_state(
    arm_state_file: str,
    caps: FuseCaps,
    *,
    expected_writer: str = ARM_WRITER,
    expected_ack: str = LIVE_ACK_PHRASE,
    expected_tenant: str | None = None,
) -> dict[str, Any]:
    """Validate the single-source arm-state control file.

    The operator writes this file, by hand or through their own tooling; MarketFlow
    code only ever READS it and never self-arms. It is the single authority for dry_run vs live
    + mode. Valid (armed) only when the file exists, parses, was written_by the
    bridge, carries the exact live_ack phrase, has mode in {exit_only, full},
    armed is true, and is not expired. Caps may only TIGHTEN: a cap that EXCEEDS
    the code cap is treated as tampering and force-disarms (caps only move down).

    Guardian mandates pass their own writer/ack
    expectations plus expected_tenant, which additionally binds the file's
    tenant_id — a tenant arm file copied into another tenant's namespace can
    never arm it. Defaults keep the single-account semantics bit-for-bit.
    """
    state: dict[str, Any] = {
        "arm_state_file": arm_state_file,
        "present": os.path.exists(arm_state_file),
        "armed": False,
        "mode": "off",
        "valid": False,
        "effective_max_total_deploy_usd": caps.max_total_deploy_usd,
        "effective_max_per_trade_usd": caps.max_per_trade_usd,
        "effective_max_drawdown_usd": caps.max_drawdown_usd,
    }
    if not state["present"]:
        state["reason"] = "no arm-state file; live execution forced to dry_run"
        return state
    try:
        with open(arm_state_file, encoding="utf-8") as f:
            arm = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        state["reason"] = f"arm-state unreadable ({exc}); live execution forced to dry_run"
        return state
    if not isinstance(arm, dict):
        state["reason"] = "arm-state is not a JSON object; live execution forced to dry_run"
        return state

    written_by = safe_str(arm.get("written_by"))
    state["written_by"] = written_by
    state["written_by_ok"] = written_by == expected_writer
    state["phrase_present"] = arm.get("live_ack") == expected_ack
    tenant_ok = True
    if expected_tenant is not None:
        tenant_ok = safe_str(arm.get("tenant_id")) == expected_tenant
        state["tenant_id"] = safe_str(arm.get("tenant_id"))
        state["tenant_ok"] = tenant_ok
    mode = str(arm.get("mode") or "off").strip().lower()
    state["mode"] = mode if mode in ARM_MODES else "off"
    armed_flag = bool(arm.get("armed"))
    state["armed_flag"] = armed_flag

    expires_at = safe_str(arm.get("expires_at"))
    expired = None
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            expired = exp < datetime.now(timezone.utc)
        except ValueError:
            # Fail closed: an unparseable expiry can never open a live window.
            expired = True
    state["expires_at"] = expires_at
    state["expired"] = expired

    budget_epoch = safe_str(arm.get("budget_epoch"))
    budget_epoch_started_at = safe_str(arm.get("budget_epoch_started_at") or arm.get("armed_at"))
    budget_epoch_time_ok = False
    if budget_epoch_started_at:
        try:
            datetime.fromisoformat(budget_epoch_started_at.replace("Z", "+00:00"))
            budget_epoch_time_ok = True
        except ValueError:
            budget_epoch_time_ok = False
    state["budget_epoch"] = budget_epoch
    state["budget_epoch_started_at"] = budget_epoch_started_at
    state["budget_epoch_explicit"] = bool(budget_epoch)
    state["budget_epoch_time_ok"] = budget_epoch_time_ok

    # A cap may go up to the CEILING (not the default); above it = tampering
    # -> disarm. For default / multi-tenant caps the ceiling EQUALS the default
    # (the default fractions), so this is identical to the old behavior. For the operator path the
    # ceiling is higher, so operator-written caps up to the ceiling are honored as
    # effective instead of being flagged as tamper. The initial
    # effective value is caps.max (the fallback default) so a missing cap field never
    # inherits the raised ceiling.
    cap_total = to_float(arm.get("max_total_deploy_usd"))
    cap_per_trade = to_float(arm.get("max_per_trade_usd"))
    cap_drawdown = to_float(arm.get("max_drawdown_usd"))
    tamper = False
    if cap_total is not None:
        if cap_total > caps.ceiling_total_usd + 1e-9:
            tamper = True
        else:
            state["effective_max_total_deploy_usd"] = cap_total
    if cap_per_trade is not None:
        if cap_per_trade > caps.ceiling_per_trade_usd + 1e-9:
            tamper = True
        else:
            state["effective_max_per_trade_usd"] = cap_per_trade
    if cap_drawdown is not None:
        if cap_drawdown > caps.ceiling_drawdown_usd + 1e-9:
            tamper = True
        else:
            state["effective_max_drawdown_usd"] = cap_drawdown
    state["cap_tamper"] = tamper

    # Expiry semantics. An expired authorisation downgrades to exit_only rather
    # than stopping everything. This is the one place in the file where "invalid"
    # does not mean "closed all the way", and the reason is worth stating:
    #
    #   An authorisation means "the system may take on NEW risk on my behalf".
    #   Expired means nobody reconfirmed it, so no new position should open. But
    #   risk already taken on must continue to be managed: stopping SELL turns an
    #   open position into an unmanaged one, which is not more conservative, it is
    #   more dangerous.
    #
    # This matches the safety posture already used elsewhere; it is not a new
    # invention:
    #   * the two separate gates GUARDIAN_LIVE_ENABLED / GUARDIAN_ENTRY_ENABLED
    #     (marketflow/guardian/store.py, entry_enabled: a single gate would force a
    #     choice between "keeps buying" and "cannot sell")
    #   * the global HALT ("BUY disabled but SELL exits remain enabled")
    #
    # **Backward compatibility is a hard requirement.** With no `expires_at`,
    # expired is None, neither branch below is taken, and behaviour is bit-for-bit
    # what it was before expiry existed. Only a file that actually carries
    # expires_at can reach the downgrade.
    core_ok = bool(
        armed_flag
        and state["mode"] in LIVE_VALID_ARM_MODES
        and state["written_by_ok"]
        and state["phrase_present"]
        and tenant_ok
        and state["budget_epoch_explicit"]
        and state["budget_epoch_time_ok"]
        and not tamper
    )
    state["expired_downgraded"] = False
    if core_ok and expired is True:
        state["expired_downgraded"] = True
        state["mode_before_expiry"] = state["mode"]
        state["mode"] = "exit_only"
        state["downgrade_reason"] = (
            "authorisation expired; downgraded to exit_only — new entries refused, "
            "exits and cancels stay available. Re-confirm to restore entry."
        )
    state["valid"] = core_ok
    state["armed"] = state["valid"]
    if not state["valid"]:
        # Surface mode 'off' for any non-live-valid file so the reported mode
        # matches the authority (no false 'full'/'exit_only' downstream).
        state["mode"] = "off"
        state["reason"] = (
            "arm-state not live-valid (disarmed / off / wrong writer / phrase / budget_epoch / expired / cap-tamper); "
            "live execution forced to dry_run"
        )
    return state


def deployed_usd_so_far(ledger_path: str) -> float:
    """Sum live, executed BUY fills from the ledger; defines remaining deploy budget."""
    if not os.path.exists(ledger_path):
        return 0.0
    total = 0.0
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                row.get("mode") == LIVE_EXECUTED_MODE
                and row.get("executed") is True
                and row.get("intent", {}).get("side") == "BUY"
            ):
                filled = to_float(row.get("fill", {}).get("filled_notional_usd"))
                if filled is not None:
                    total += filled
    return round(total, 8)


def order_idempotency_key(intent: OrderIntent) -> str:
    if intent.idempotency_key:
        return intent.idempotency_key
    seed = {
        "signal_id": intent.signal_id,
        "action": intent.action,
        "side": intent.side,
        "market_id": intent.market_id,
        "token_id": intent.token_id,
        "outcome": intent.outcome,
    }
    return "pmxidem_" + hash_obj(seed)[:24]


def executed_idempotency_keys(ledger_path: str) -> set[str]:
    """Idempotency keys of orders that CONFIRMED executed (LIVE_EXECUTED with
    executed is True). An attempt that never succeeded (orphan LIVE_PENDING, or a
    LIVE_EXECUTED with executed False) does not burn its key, so a failed or crashed
    submission is retried instead of being silently treated as already-placed."""
    keys: set[str] = set()
    for row in _ledger_rows(ledger_path):
        if row.get("mode") == LIVE_EXECUTED_MODE and row.get("executed") is True:
            key = safe_str(row.get("idempotency_key") or row.get("intent", {}).get("idempotency_key"))
            if key:
                keys.add(key)
    return keys


def _intent_data(intent: OrderIntent | dict[str, Any]) -> dict[str, Any]:
    return intent.redacted() if isinstance(intent, OrderIntent) else dict(intent)


def infer_settlement_cycle(intent: OrderIntent | dict[str, Any]) -> str:
    data = _intent_data(intent)
    explicit = safe_str(data.get("settlement_cycle"))
    if explicit:
        return explicit
    slug = safe_str(data.get("market_slug"))
    if slug:
        m = re.search(r"\d{4}-\d{2}-\d{2}", slug)
        if m:
            return m.group(0)
    return "unknown"


def exposure_key(intent: OrderIntent | dict[str, Any]) -> str | None:
    data = _intent_data(intent)
    market_ref = safe_str(data.get("market_id")) or safe_str(data.get("market_slug"))
    token = safe_str(data.get("token_id"))
    if not market_ref and token:
        market_ref = f"TOKEN:{token}"
    if not market_ref:
        return None
    outcome = safe_str(data.get("outcome"))
    if not outcome and token:
        outcome = f"TOKEN:{token}"
    if not outcome:
        return None
    return "|".join((market_ref, outcome.upper(), infer_settlement_cycle(data)))


def _row_filled_shares(row: dict[str, Any]) -> float:
    fill = row.get("fill") if isinstance(row.get("fill"), dict) else {}
    receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
    intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
    side = str(intent.get("side") or "").upper()
    explicit = to_float(fill.get("filled_shares"))
    if explicit is not None:
        return max(0.0, explicit)
    if side == "BUY":
        shares = to_float(receipt.get("taking_amount"))
    elif side == "SELL":
        shares = to_float(receipt.get("making_amount"))
    else:
        shares = None
    if shares is None:
        shares = to_float(intent.get("size"))
    return max(0.0, shares or 0.0)


def _row_is_live_executed(row: dict[str, Any]) -> bool:
    return row.get("mode") == LIVE_EXECUTED_MODE and row.get("executed") is True


def _ledger_rows(ledger_path: str) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_path):
        return []
    rows: list[dict[str, Any]] = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def existing_open_buy_exposure_keys(ledger_path: str) -> set[str]:
    """Markets/outcomes holding a net-open position from CONFIRMED fills only.
    Exposure = executed BUY shares minus executed SELL shares. A LIVE_PENDING
    attempt never counts, so a never-filled order cannot block a fresh entry; a
    market sold back to flat is released for re-entry."""
    net: dict[str, float] = {}
    for row in _ledger_rows(ledger_path):
        if not _row_is_live_executed(row):
            continue
        intent = row.get("intent", {})
        key = exposure_key(intent)
        if not key:
            continue
        side = str(intent.get("side") or "").upper()
        shares = _row_filled_shares(row)
        if side == "BUY":
            net[key] = net.get(key, 0.0) + shares
        elif side == "SELL":
            net[key] = net.get(key, 0.0) - shares
    return {key for key, shares in net.items() if shares > 1e-9}


def existing_live_buy_tokens(ledger_path: str) -> set[str]:
    """Tokens holding a net-open BUY position from CONFIRMED fills only. A
    LIVE_PENDING attempt does not lock the token, and a token fully sold back to
    flat is released for re-entry."""
    net: dict[str, float] = {}
    for row in _ledger_rows(ledger_path):
        if not _row_is_live_executed(row):
            continue
        intent = row.get("intent", {})
        token = safe_str(intent.get("token_id"))
        if not token:
            continue
        side = str(intent.get("side") or "").upper()
        shares = _row_filled_shares(row)
        if side == "BUY":
            net[token] = net.get(token, 0.0) + shares
        elif side == "SELL":
            net[token] = net.get(token, 0.0) - shares
    return {token for token, shares in net.items() if shares > 1e-9}


def evaluate_fuses(
    intent: OrderIntent,
    *,
    requested_live: bool,
    caps: FuseCaps,
    kill_file: str,
    arm_state_file: str,
    deployed_before_usd: float,
    arm_expected_writer: str = ARM_WRITER,
    arm_expected_ack: str = LIVE_ACK_PHRASE,
    arm_expected_tenant: str | None = None,
) -> FuseResult:
    refusals: list[str] = []
    checks: dict[str, Any] = {}

    kill = kill_switch_state(kill_file)
    checks["kill_switch"] = kill

    arm = load_arm_state(
        arm_state_file, caps,
        expected_writer=arm_expected_writer,
        expected_ack=arm_expected_ack,
        expected_tenant=arm_expected_tenant,
    )
    checks["arm_state"] = arm

    effective_total_cap = arm.get("effective_max_total_deploy_usd", caps.max_total_deploy_usd)
    effective_per_trade_cap = arm.get("effective_max_per_trade_usd", caps.max_per_trade_usd)
    effective_drawdown_cap = arm.get("effective_max_drawdown_usd", caps.max_drawdown_usd)
    checks["caps"] = {
        "max_total_deploy_usd_code": caps.max_total_deploy_usd,
        "max_total_deploy_usd_effective": effective_total_cap,
        "max_per_trade_usd_code": caps.max_per_trade_usd,
        "max_per_trade_usd_effective": effective_per_trade_cap,
        "max_drawdown_usd_code": caps.max_drawdown_usd,
        "max_drawdown_usd_effective": effective_drawdown_cap,
    }

    # Fuse 3: dry_run is default; live needs requested_live AND a valid armed
    # arm-state AND the arm mode permitting this side AND no kill switch. Any
    # miss forces dry_run rather than refusing outright.
    arm_valid = bool(arm.get("valid"))
    arm_mode = arm.get("mode", "off")
    mode_permits = arm_mode_permits(arm_mode, intent.side)
    # A global HALT created by account-budget reconciliation must stop new BUY
    # exposure, but it must not trap the operator in a losing position. Explicit kill
    # inputs (kill file/env) still block both sides.
    kill_blocks_live = bool(kill["active"] or (intent.side == "BUY" and kill.get("global_halt_set")))
    forced_dry_run = not (requested_live and arm_valid and mode_permits and not kill_blocks_live)
    checks["execution_mode_decision"] = {
        "requested_live": requested_live,
        "arm_valid": arm_valid,
        "arm_mode": arm_mode,
        "mode_permits_side": mode_permits,
        "kill_switch_active": kill["active"],
        "global_halt_set": kill.get("global_halt_set"),
        "kill_blocks_live": kill_blocks_live,
        "forced_dry_run": forced_dry_run,
    }

    # Fuse 1: capital caps (BUY/deploy only). SELL exits are never deploy-gated.
    notional = deploy_notional_usd(intent)
    remaining = round(effective_total_cap - deployed_before_usd, 8)
    if intent.side == "BUY":
        if notional is None:
            refusals.append("buy notional could not be computed; refusing to deploy capital blindly")
        else:
            if notional > effective_per_trade_cap + 1e-9:
                refusals.append(
                    f"per-trade cap breached: notional ${notional} > ${effective_per_trade_cap}"
                )
            if deployed_before_usd + (notional or 0.0) > effective_total_cap + 1e-9:
                refusals.append(
                    f"total deploy cap breached: ${deployed_before_usd} + ${notional} "
                    f"> ${effective_total_cap}"
                )
        taker_reason = safe_str(intent.taker_allowed_reason)
        if intent.order_kind == "market":
            if taker_reason != "in_play_speed_window":
                refusals.append("market BUY/taker entry requires taker_allowed_reason=in_play_speed_window")
        elif not intent.post_only:
            if taker_reason != "in_play_speed_window":
                refusals.append("non-post-only BUY entry requires taker_allowed_reason=in_play_speed_window")
        # Price-expression floor. The worst price this order can pay is the limit
        # price (limit) or max_price (market) — that is what the floor binds on.
        floor_px = to_float(intent.price) if intent.order_kind == "limit" else to_float(intent.max_price)
        floor_override = safe_str(intent.price_floor_override_reason)
        floor_breached = floor_px is not None and floor_px < BUY_PRICE_FLOOR and not floor_override
        checks["buy_price_floor"] = {
            "price_floor": BUY_PRICE_FLOOR,
            "worst_entry_price": floor_px,
            "basis": "limit_price" if intent.order_kind == "limit" else "max_price",
            "breached": floor_breached,
            "enforced": BUY_PRICE_FLOOR_ENFORCE,
            "override_reason": floor_override or None,
        }
        if floor_breached and BUY_PRICE_FLOOR_ENFORCE:
            refusals.append(
                f"BUY price floor: entry price {floor_px} below {BUY_PRICE_FLOOR} "
                ""
            )
    else:
        # Fuse 4 (exit-first) + no naked short: SELL size must not exceed held shares.
        size = to_float(intent.size)
        held = to_float(intent.held_shares)
        if size is None or size <= 0:
            refusals.append("sell size must be positive")
        elif held is None:
            refusals.append("sell requires known held_shares to forbid naked short")
        elif size > held + 1e-9:
            refusals.append(f"naked short refused: sell size {size} > held shares {held}")

    # Order-shape sanity (applies to both sides).
    if intent.order_kind == "limit":
        price = to_float(intent.price)
        if price is None or not (MIN_LIMIT_PRICE <= price <= MAX_LIMIT_PRICE):
            refusals.append(
                f"limit price must be within [{MIN_LIMIT_PRICE}, {MAX_LIMIT_PRICE}]; got {intent.price}"
            )
        if to_float(intent.size) is None or to_float(intent.size) <= 0:
            refusals.append("limit order size must be positive")
    else:
        if intent.market_order_type not in ("FAK", "FOK"):
            refusals.append("market order_type must be FAK or FOK")
        if intent.side == "BUY" and to_float(intent.max_price) is None:
            refusals.append("market BUY requires max_price; unbounded market orders are forbidden")
        if intent.side == "SELL" and to_float(intent.min_price) is None:
            refusals.append("market SELL requires min_price; unbounded market orders are forbidden")

    passed = len(refusals) == 0
    return FuseResult(
        passed=passed,
        forced_dry_run=forced_dry_run,
        kill_switch_active=kill_blocks_live,
        arm_valid=arm_valid,
        arm_mode=arm_mode,
        checks=checks,
        refusals=refusals,
        deploy_notional_usd=notional,
        deployed_before_usd=deployed_before_usd,
        remaining_deploy_usd=remaining,
    )


def normalize_book_levels(book: Any, side_key: str) -> list[tuple[float, float]]:
    """Normalize CLOB order-book levels to (price, size); bids high-first, asks low-first."""
    raw = attr(book, side_key)
    levels: list[tuple[float, float]] = []
    for level in raw or []:
        price = to_float(attr(level, "price"))
        size = to_float(attr(level, "size"))
        if price is None or size is None or price < 0 or size <= 0:
            continue
        levels.append((price, size))
    return sorted(levels, key=lambda x: x[0], reverse=(side_key == "bids"))


def sweep_levels(levels: list[tuple[float, float]], target_shares: float) -> dict[str, Any]:
    """Walk price levels filling target_shares; returns fill estimate."""
    remaining = target_shares
    value = 0.0
    filled = 0.0
    consumed: list[dict[str, float]] = []
    best = levels[0][0] if levels else None
    terminal = None
    for price, size in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, size)
        value += take * price
        filled += take
        remaining -= take
        terminal = price
        consumed.append({"price": round(price, 8), "shares": round(take, 8)})
    full = remaining <= 1e-9
    avg = value / filled if filled > 0 else None
    return {
        "requested_shares": round(target_shares, 8),
        "filled_shares": round(filled, 8),
        "unfilled_shares": round(max(0.0, remaining), 8),
        "gross_value_usd": round(value, 8),
        "average_price": round(avg, 8) if avg is not None else None,
        "best_price": round(best, 8) if best is not None else None,
        "terminal_price": round(terminal, 8) if terminal is not None else None,
        "full_fill": full,
        "consumed_levels": consumed,
    }


def estimate_fill(intent: OrderIntent, book: Any) -> dict[str, Any]:
    """Read-only expected-fill estimate from a CLOB order book (no SDK order calls).

    The estimate honors the order's price constraints: a limit SELL only fills
    bids >= its price, a limit BUY only fills asks <= its price, and market
    orders respect min_price/max_price. The exchange sets the true fill at
    execution; this is an advisory context number only.
    """
    if intent.side == "SELL":
        bids = normalize_book_levels(book, "bids")
        floor = to_float(intent.price) if intent.order_kind == "limit" else to_float(intent.min_price)
        if floor is not None:
            bids = [(p, s) for p, s in bids if p >= floor - 1e-12]
        size = to_float(intent.size) or 0.0
        sweep = sweep_levels(bids, size)
        return {
            "side": "SELL",
            "basis": "sweep_bids_limit" if intent.order_kind == "limit" else "sweep_bids_market",
            "price_floor": floor,
            "estimated_proceeds_usd": sweep["gross_value_usd"] if sweep["full_fill"] else None,
            "partial_proceeds_usd": sweep["gross_value_usd"],
            "average_price": sweep["average_price"],
            "full_fill": sweep["full_fill"],
            "sweep": sweep,
        }
    asks = normalize_book_levels(book, "asks")
    ceil = to_float(intent.price) if intent.order_kind == "limit" else to_float(intent.max_price)
    if ceil is not None:
        asks = [(p, s) for p, s in asks if p <= ceil + 1e-12]
    if intent.order_kind == "limit":
        target = to_float(intent.size) or 0.0
        sweep = sweep_levels(asks, target)
        return {
            "side": "BUY",
            "basis": "sweep_asks_limit",
            "price_ceiling": ceil,
            "estimated_cost_usd": sweep["gross_value_usd"] if sweep["full_fill"] else None,
            "partial_cost_usd": sweep["gross_value_usd"],
            "average_price": sweep["average_price"],
            "full_fill": sweep["full_fill"],
            "sweep": sweep,
        }
    # market BUY: walk affordable asks (<= max_price) until max_spend is exhausted
    budget = to_float(intent.max_spend_usd) or 0.0
    spent = 0.0
    shares = 0.0
    terminal = None
    consumed: list[dict[str, float]] = []
    for price, size in asks:
        if spent >= budget - 1e-9 or price <= 0:
            break
        affordable = (budget - spent) / price
        take = min(size, affordable)
        if take <= 0:
            break
        spent += take * price
        shares += take
        terminal = price
        consumed.append({"price": round(price, 8), "shares": round(take, 8)})
    avg = spent / shares if shares > 0 else None
    return {
        "side": "BUY",
        "basis": "spend_asks_market",
        "price_ceiling": ceil,
        "estimated_cost_usd": round(spent, 8),
        "estimated_shares": round(shares, 8),
        "average_price": round(avg, 8) if avg is not None else None,
        "terminal_price": round(terminal, 8) if terminal is not None else None,
        "budget_usd": round(budget, 8),
        "consumed_levels": consumed,
    }


def execution_quality_estimate(intent: OrderIntent, book: Any = None) -> dict[str, Any]:
    """Maker/taker execution-quality estimate for scorecards."""
    best_bid = None
    best_ask = None
    spread = None
    if book is not None:
        bids = normalize_book_levels(book, "bids")
        asks = normalize_book_levels(book, "asks")
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        if best_bid is not None and best_ask is not None:
            spread = max(0.0, best_ask - best_bid)

    maker_candidate = intent.order_kind == "limit" and bool(intent.post_only)
    size = to_float(intent.size)
    limit_price = to_float(intent.price)
    taker_px = best_ask if intent.side == "BUY" else best_bid
    if taker_px is None:
        taker_px = to_float(intent.max_price if intent.side == "BUY" else intent.min_price)
    maker_cost = limit_price * size if limit_price is not None and size is not None else None
    taker_cost = taker_px * size if taker_px is not None and size is not None else None
    saved = None
    if intent.side == "BUY" and maker_cost is not None and taker_cost is not None:
        saved = taker_cost - maker_cost
    elif intent.side == "SELL" and maker_cost is not None and taker_cost is not None:
        saved = maker_cost - taker_cost
    return {
        "policy": "maker_first" if maker_candidate else "taker_allowed" if intent.taker_allowed_reason else "taker_or_market",
        "maker_candidate": maker_candidate,
        "post_only": bool(intent.post_only),
        "taker_allowed_reason": intent.taker_allowed_reason,
        "best_bid": round(best_bid, 8) if best_bid is not None else None,
        "best_ask": round(best_ask, 8) if best_ask is not None else None,
        "spread": round(spread, 8) if spread is not None else None,
        "limit_price": round(limit_price, 8) if limit_price is not None else None,
        "size": round(size, 8) if size is not None else None,
        "taker_baseline_price": round(taker_px, 8) if taker_px is not None else None,
        "maker_notional_usd": round(maker_cost, 8) if maker_cost is not None else None,
        "taker_baseline_notional_usd": round(taker_cost, 8) if taker_cost is not None else None,
        "estimated_spread_saved_usd": round(max(0.0, saved), 8) if saved is not None else None,
    }


def plan_order(
    intent: OrderIntent,
    *,
    requested_live: bool,
    caps: FuseCaps,
    kill_file: str,
    arm_state_file: str,
    ledger_path: str,
    book: Any = None,
    arm_expected_writer: str = ARM_WRITER,
    arm_expected_ack: str = LIVE_ACK_PHRASE,
    arm_expected_tenant: str | None = None,
) -> dict[str, Any]:
    """Pure planning: fuse gate + read-only fill estimate. Never signs or submits."""
    deployed = deployed_usd_so_far(ledger_path)
    idem_key = order_idempotency_key(intent)
    fuses = evaluate_fuses(
        intent,
        requested_live=requested_live,
        caps=caps,
        kill_file=kill_file,
        arm_state_file=arm_state_file,
        deployed_before_usd=deployed,
        arm_expected_writer=arm_expected_writer,
        arm_expected_ack=arm_expected_ack,
        arm_expected_tenant=arm_expected_tenant,
    )
    if idem_key in executed_idempotency_keys(ledger_path):
        fuses.refusals.append("duplicate idempotency key; an order for this signal/market/side already executed")
        fuses.passed = False
        fuses.checks["idempotency"] = {"idempotency_key": idem_key, "duplicate": True}
    else:
        fuses.checks["idempotency"] = {"idempotency_key": idem_key, "duplicate": False}
    # Adding to an existing position is allowed — MarketFlow decides whether to add; the
    # caps (total/per-trade) bound exposure. Prior exposure on the same market/token
    # is recorded for visibility but never refused on; the idempotency fuse above
    # still blocks re-executing the exact same signal.
    open_key = exposure_key(intent)
    if intent.side == "BUY" and open_key:
        prior = open_key in existing_open_buy_exposure_keys(ledger_path)
        fuses.checks["open_market_exposure"] = {"exposure_key": open_key, "prior_exposure": prior}
    buy_token = safe_str(intent.token_id) if intent.side == "BUY" else None
    if buy_token:
        prior = buy_token in existing_live_buy_tokens(ledger_path)
        fuses.checks["live_buy_token"] = {"token_id": buy_token, "prior_position": prior}
    will_execute_live = bool(requested_live and not fuses.forced_dry_run and fuses.passed)
    fill_estimate = estimate_fill(intent, book) if book is not None else None
    execution_quality = execution_quality_estimate(intent, book)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "plan_id": f"pmx_{now_ms()}_{hash_obj(intent.redacted())[:10]}",
        "idempotency_key": idem_key,
        "mode": "LIVE_PLAN" if will_execute_live else "DRY_RUN_PLAN",
        "boundaries": EXECUTION_BOUNDARIES,
        "intent": intent.redacted(),
        "fuses": {
            "passed": fuses.passed,
            "forced_dry_run": fuses.forced_dry_run,
            "kill_switch_active": fuses.kill_switch_active,
            "arm_valid": fuses.arm_valid,
            "arm_mode": fuses.arm_mode,
            "refusals": fuses.refusals,
            "deploy_notional_usd": fuses.deploy_notional_usd,
            "deployed_before_usd": fuses.deployed_before_usd,
            "remaining_deploy_usd": fuses.remaining_deploy_usd,
            "checks": fuses.checks,
        },
        "will_execute_live": will_execute_live,
        "would_place": fuses.passed,
        "fill_estimate": fill_estimate,
        "execution_quality": execution_quality,
    }


def detect_wallet(client: Any, expected_wallet_type: str) -> dict[str, Any]:
    """Detect + assert the funder wallet type before any live action."""
    wallet_type = safe_str(attr(client, "wallet_type"))
    signer = mask_address(attr(client, "wallet"))
    result = {
        "wallet_type": wallet_type,
        "wallet_type_expected": expected_wallet_type,
        "wallet_type_known": wallet_type in WALLET_TYPES,
        "wallet_type_matches": wallet_type == expected_wallet_type,
        "funder_masked": signer,
    }
    return result


def redact_signed_order(signed: Any) -> dict[str, Any]:
    """Ledger-safe view of a SignedOrder; signer/maker masked, signature dropped."""
    signature = attr(signed, "signature")
    return {
        "token_id": safe_str(attr(signed, "token_id")),
        "side": safe_str(attr(signed, "side")),
        "order_type": safe_str(attr(signed, "order_type")),
        "signature_type": attr(signed, "signature_type"),
        "maker_amount": rounded_float(attr(signed, "maker_amount")),
        "taker_amount": rounded_float(attr(signed, "taker_amount")),
        "expiration": attr(signed, "expiration"),
        "post_only": attr(signed, "post_only"),
        "signer_masked": mask_address(attr(signed, "signer")),
        "maker_masked": mask_address(attr(signed, "maker")),
        "signature_present": signature is not None and str(signature).strip() != "",
        "signature_len": len(str(signature)) if signature is not None else 0,
    }


def redact_order_response(resp: Any) -> dict[str, Any]:
    """Ledger-safe view of an AcceptedOrder / RejectedOrder receipt."""
    ok = attr(resp, "ok")
    if ok is True or attr(resp, "order_id") is not None:
        making = rounded_float(attr(resp, "making_amount"))
        taking = rounded_float(attr(resp, "taking_amount"))
        return {
            "ok": True,
            "order_id": safe_str(attr(resp, "order_id")),
            "status": safe_str(attr(resp, "status")),
            "making_amount": making,
            "taking_amount": taking,
            "trade_ids": [safe_str(t) for t in (attr(resp, "trade_ids") or [])],
            "transactions_hashes": [safe_str(t) for t in (attr(resp, "transactions_hashes") or [])],
        }
    return {
        "ok": False,
        "code": safe_str(attr(resp, "code")),
        "message": safe_str(attr(resp, "message")),
    }


def snap_price_to_tick(client: Any, token_id: str, price: Decimal, side: str) -> Decimal:
    """Snap a limit price onto a multiple of the market's tick, conservatively:
    BUY rounds down and SELL rounds up, so the snapped price never crosses the
    intended one. A BUY never pays more than asked, and a SELL's "at least this
    much" floor never drops.

    Why it is needed: a limit price derived from a mid can carry arbitrary decimal
    places, while the venue requires a multiple of minimum_tick_size and rejects
    anything else outright. A failed tick lookup returns the price unchanged and
    lets the venue's own validation decide — fail soft, never a new refusal
    path."""
    try:
        from polymarket._internal.actions.orders.market_data import fetch_tick_size_sync

        tick = fetch_tick_size_sync(client._ctx, token_id=token_id)
        if tick and tick > 0:
            rounding = ROUND_FLOOR if str(side).upper() == "BUY" else ROUND_CEILING
            snapped = ((price / tick).to_integral_value(rounding=rounding) * tick).quantize(tick)
            if Decimal("0") < snapped < Decimal("1"):
                return snapped
    except Exception:  # noqa: BLE001 - unknown tick: let the SDK validate as-is
        pass
    return price


def build_signed_order(client: Any, intent: OrderIntent) -> Any:
    """LIVE: construct + EIP-712-sign the order via the official SDK (no submit)."""
    if intent.order_kind == "limit":
        return client.create_limit_order(
            token_id=intent.token_id,
            price=snap_price_to_tick(client, intent.token_id, Decimal(str(intent.price)), intent.side),
            size=Decimal(str(intent.size)),
            side=intent.side,
            post_only=intent.post_only,
            expiration=intent.expiration,
        )
    if intent.side == "SELL":
        return client.create_market_order(
            token_id=intent.token_id,
            side="SELL",
            shares=Decimal(str(intent.size)),
            min_price=Decimal(str(intent.min_price)) if intent.min_price is not None else None,
            order_type=intent.market_order_type,
        )
    return client.create_market_order(
        token_id=intent.token_id,
        side="BUY",
        amount=Decimal(str(intent.max_spend_usd)),
        max_price=Decimal(str(intent.max_price)) if intent.max_price is not None else None,
        order_type=intent.market_order_type,
    )


def fill_from_receipt(receipt: dict[str, Any], intent: OrderIntent) -> dict[str, Any]:
    """Derive realized fill notional/price from a redacted order receipt.

    `accepted` = the exchange took the order. `filled` = shares actually changed
    hands. These are NOT the same event and post-only maker orders are exactly
    where they come apart: the receipt comes back `status="live"` with
    `making_amount=0`, `taking_amount=0`, `trade_ids=[]` — an order RESTING on the
    book, nothing traded. `filled` used to include `"live"`, so every resting
    maker order was recorded as a fill with zero filled shares, and any fill rate
    computed off this ledger read 100% for a path whose real rate was lower.
    Monetary paths were never affected — they read
    `filled_notional_usd`, which was correctly 0 — but every human and every
    downstream report reading `filled` was being told the wrong thing.

    `resting` is surfaced alongside so the resting case stays visible instead of
    collapsing into a bare False.
    """
    if not receipt.get("ok"):
        return {"filled": False, "accepted": False, "resting": False,
                "filled_notional_usd": 0.0, "reason": receipt.get("message")}
    making = to_float(receipt.get("making_amount"))
    taking = to_float(receipt.get("taking_amount"))
    notional = None
    avg_price = None
    if intent.side == "BUY":
        notional = making
        filled_shares = taking
        if making is not None and taking not in (None, 0):
            avg_price = round(making / taking, 8)
    else:
        notional = taking
        filled_shares = making
        if taking is not None and making not in (None, 0):
            avg_price = round(taking / making, 8)
    status = receipt.get("status")
    return {
        # "matched" = traded. "delayed" = marketable and awaiting the matching
        # delay, so it is a fill in flight. "live" = resting on the book, not one.
        "filled": status in ("matched", "delayed"),
        "resting": status == "live",
        "accepted": True,
        "status": status,
        "filled_notional_usd": round(notional, 8) if notional is not None else None,
        "filled_shares": round(filled_shares, 8) if filled_shares is not None else None,
        "average_price": avg_price,
        "order_id": receipt.get("order_id"),
        "trade_ids": receipt.get("trade_ids"),
    }


def confirm_fill(client: Any, *, order_id: str, market_id: str | None = None, token_id: str | None = None) -> dict[str, Any]:
    """Read fills back via the read-only SDK methods."""
    result: dict[str, Any] = {"order_id": order_id, "read_only": True}
    try:
        order = client.get_order(order_id=order_id)
        result["order"] = {
            "id": safe_str(attr(order, "id")),
            "status": safe_str(attr(order, "status")),
            "size_matched": rounded_float(attr(order, "size_matched")),
            "original_size": rounded_float(attr(order, "original_size")),
            "price": rounded_float(attr(order, "price")),
        }
    except Exception as exc:  # pragma: no cover - depends on live SDK/network.
        result["order_error"] = safe_str(exc)
    trades: list[dict[str, Any]] = []
    try:
        paginator = client.list_account_trades(market=market_id) if market_id else client.list_account_trades(token_id=token_id)
        for page in paginator:
            for trade in attr(page, "items") or []:
                trades.append(
                    {
                        "trade_id": safe_str(attr(trade, "id")),
                        "side": safe_str(attr(trade, "side")),
                        "price": rounded_float(attr(trade, "price")),
                        "size": rounded_float(attr(trade, "size")),
                        "status": safe_str(attr(trade, "status")),
                        "transaction_hash": safe_str(attr(trade, "transaction_hash")),
                    }
                )
            break  # first page is enough for a fill confirmation
    except Exception as exc:  # pragma: no cover - depends on live SDK/network.
        result["trades_error"] = safe_str(exc)
    result["recent_trades"] = trades
    return result


def parse_time(value: Any) -> datetime | None:
    text = safe_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_paginator_items(paginator: Any, *, max_pages: int = 10) -> list[Any]:
    items: list[Any] = []
    pages = 0
    for page in paginator:
        pages += 1
        for item in attr(page, "items") or []:
            items.append(item)
        if pages >= max_pages or not bool(attr(page, "has_more")):
            break
    return items


def ledger_known_execution_refs(ledger_path: str) -> dict[str, set[str]]:
    refs = {"trade_ids": set(), "order_ids": set(), "transaction_hashes": set()}
    if not os.path.exists(ledger_path):
        return refs
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fill = row.get("fill") if isinstance(row.get("fill"), dict) else {}
            receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
            for trade_id in fill.get("trade_ids") or receipt.get("trade_ids") or []:
                trade = safe_str(trade_id)
                if trade:
                    refs["trade_ids"].add(trade)
            order_id = safe_str(fill.get("order_id") or receipt.get("order_id"))
            if order_id:
                refs["order_ids"].add(order_id)
            for tx in receipt.get("transactions_hashes") or []:
                tx_hash = safe_str(tx)
                if tx_hash:
                    refs["transaction_hashes"].add(tx_hash)
    return refs


def _trade_order_ids(trade: Any) -> set[str]:
    order_ids: set[str] = set()
    for name in ("taker_order_id", "order_id"):
        order_id = safe_str(attr(trade, name))
        if order_id:
            order_ids.add(order_id)
    for maker in attr(trade, "maker_orders") or []:
        order_id = safe_str(attr(maker, "order_id"))
        if order_id:
            order_ids.add(order_id)
    return order_ids


def account_budget_snapshot(client: Any, *, since_utc: str, ledger_path: str) -> dict[str, Any]:
    since = parse_time(since_utc)
    if since is None:
        raise ExecutionError("budget_epoch_started_at is missing or invalid; refusing live budget check")
    known_refs = ledger_known_execution_refs(ledger_path)
    deployed = 0.0
    unknown_fills: list[dict[str, Any]] = []
    try:
        trades = _iter_paginator_items(client.list_account_trades(), max_pages=10)
    except Exception as exc:
        raise ExecutionError(f"account trade read failed before live order: {exc}") from exc
    for trade in trades:
        matched_at = parse_time(attr(trade, "matched_at"))
        if matched_at is not None and matched_at < since:
            continue
        side = str(attr(trade, "side") or "").upper()
        price = to_float(attr(trade, "price"))
        size = to_float(attr(trade, "size"))
        trade_id = safe_str(attr(trade, "id"))
        trade_order_ids = _trade_order_ids(trade)
        transaction_hash = safe_str(attr(trade, "transaction_hash"))
        if side == "BUY" and price is not None and size is not None:
            deployed += price * size
        known_trade = bool(
            (trade_id and trade_id in known_refs["trade_ids"])
            or (trade_order_ids & known_refs["order_ids"])
            or (transaction_hash and transaction_hash in known_refs["transaction_hashes"])
        )
        if trade_id and not known_trade:
            unknown_fills.append(
                {
                    "trade_id": trade_id,
                    "order_ids": sorted(trade_order_ids),
                    "transaction_hash": transaction_hash,
                    "side": side,
                    "price": rounded_float(price),
                    "size": rounded_float(size),
                    "matched_at": safe_str(attr(trade, "matched_at")),
                }
            )

    pending_reserve = 0.0
    try:
        orders = _iter_paginator_items(client.list_open_orders(), max_pages=10)
    except Exception as exc:
        raise ExecutionError(f"open-order read failed before live order: {exc}") from exc
    for order in orders:
        side = str(attr(order, "side") or "").upper()
        if side != "BUY":
            continue
        price = to_float(attr(order, "price")) or 0.0
        original = to_float(attr(order, "original_size")) or 0.0
        matched = to_float(attr(order, "size_matched")) or 0.0
        pending_reserve += max(0.0, original - matched) * price

    at_risk = 0.0
    try:
        positions = _iter_paginator_items(client.list_positions(), max_pages=10)
    except Exception as exc:
        raise ExecutionError(f"position read failed before live order: {exc}") from exc
    for pos in positions:
        current_value = to_float(attr(pos, "current_value"))
        initial_value = to_float(attr(pos, "initial_value"))
        if current_value is not None:
            at_risk += max(0.0, current_value)
        elif initial_value is not None:
            at_risk += max(0.0, initial_value)
    return {
        "since_utc": since_utc,
        "deployed_buy_fills_usd": round(deployed, 8),
        "pending_buy_reserve_usd": round(pending_reserve, 8),
        "at_risk_position_value_usd": round(at_risk, 8),
        "unknown_fills": unknown_fills,
        "known_trade_count": len(known_refs["trade_ids"]),
        "known_order_count": len(known_refs["order_ids"]),
        "known_transaction_count": len(known_refs["transaction_hashes"]),
    }


DATA_API_BASE = "https://data-api.polymarket.com"


def read_funder_address(secret_dir: str = DEFAULT_SECRET_DIR) -> str:
    """Read the PUBLIC funder (proxy wallet) address ref. The address is public
    onchain data — reading it is not a secret disclosure; it is never printed
    unmasked into ledgers by callers."""
    addr = (read_secret_ref(secret_dir, SECRET_REFS["funder_address"]) or "").strip()
    if not addr:
        raise ExecutionError("funder address ref missing/empty; cannot rebuild drawdown")
    return addr


def account_realized_flows(
    funder_address: str,
    *,
    since_utc: str,
    timeout: float = 20.0,
    max_pages: int = 10,
) -> dict[str, Any]:
    """drawdown fuse input: rebuild epoch USDC flows from the PUBLIC
    data-api activity feed (read-only, unauthenticated, keyed by the public
    funder address).

    Returns BUY/SELL/REDEEM usdc sums inside the epoch window. SPLIT counts as
    buy-side outflow and MERGE as sell-side inflow (conservative equivalents);
    unknown types are counted and ignored (missing inflow only OVERSTATES the
    loss → fuse trips earlier, never later). Any fetch/parse failure raises
    ExecutionError — fail-closed: no drawdown number, no live BUY.
    """
    since = parse_time(since_utc)
    if since is None:
        raise ExecutionError("budget_epoch_started_at missing/invalid; refusing drawdown rebuild")
    since_ts = since.timestamp()
    buy = sell = redeem = 0.0
    ignored: dict[str, int] = {}
    offset = 0
    for _page in range(max_pages):
        url = f"{DATA_API_BASE}/activity?user={funder_address}&limit=500&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "marketflow-drawdown"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise ExecutionError(f"activity read failed before live BUY: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            break
        crossed_window = False
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = to_float(r.get("timestamp"))
            if ts is None or ts < since_ts:
                crossed_window = True
                continue
            typ = str(r.get("type") or "").upper()
            usdc = to_float(r.get("usdcSize")) or 0.0
            side = str(r.get("side") or "").upper()
            if typ == "TRADE":
                if side == "BUY":
                    buy += usdc
                elif side == "SELL":
                    sell += usdc
            elif typ == "REDEEM":
                redeem += usdc
            elif typ == "SPLIT":
                buy += usdc
            elif typ == "MERGE":
                sell += usdc
            else:
                ignored[typ] = ignored.get(typ, 0) + 1
        if crossed_window or len(rows) < 500:
            break
        offset += len(rows)
    return {
        "buy_usd": round(buy, 8),
        "sell_usd": round(sell, 8),
        "redeem_usd": round(redeem, 8),
        "ignored_types": ignored,
        "source": "data_api_activity_public",
    }


def engage_global_halt(reason: str, *, halt_file: str | None = None) -> None:
    if halt_file is None:
        halt_file = DEFAULT_GLOBAL_HALT_FILE
    ensure_parent(halt_file)
    with open(halt_file, "w", encoding="utf-8") as f:
        f.write(f"{iso_now()} {reason}\n")


def pre_live_account_budget_check(
    client: Any,
    intent: OrderIntent,
    plan: dict[str, Any],
    *,
    ledger_path: str,
    halt_file: str | None = None,
    secret_dir: str = DEFAULT_SECRET_DIR,
    realized_flows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """net-exposure budget semantics (supersedes cumulative-fills cap).

    BUY gates, all rebuilt from ACCOUNT TRUTH (never local bookkeeping alone):
      1. unknown-fill gate — any fill not in our ledger since epoch start refuses
         this BUY (reconciliation drift protection; unchanged).
      2. NET-EXPOSURE cap (primary budget) — at_risk positions + pending BUY
         reserve + this order ≤ total cap. Settlement RELEASES budget, so wins
         compound without a manual epoch reopen.
      3. DRAWDOWN fuse (loss hard ceiling) — epoch realized loss = buys − sells
         − redeems − at_risk (public data-api activity flows) ≥ max_drawdown →
         BUY-only HALT (exits/redeems unaffected). This is what bounds total
         loss now that the cumulative-fills cap is gone: net-exposure alone
         would allow lose-$cap → release → lose again, unbounded.
    SELL/exit paths never consume budget and are never blocked here.
    `realized_flows` is a test-injection override; None → live data-api rebuild.
    """
    if halt_file is None:
        halt_file = DEFAULT_GLOBAL_HALT_FILE
    arm = plan.get("fuses", {}).get("checks", {}).get("arm_state", {})
    caps = plan.get("fuses", {}).get("checks", {}).get("caps", {})
    total_cap = to_float(caps.get("max_total_deploy_usd_effective")) or DEFAULT_MAX_TOTAL_DEPLOY_USD
    drawdown_cap = to_float(caps.get("max_drawdown_usd_effective")) or DEFAULT_MAX_DRAWDOWN_USD
    since = safe_str(arm.get("budget_epoch_started_at"))
    snapshot = account_budget_snapshot(client, since_utc=since or "", ledger_path=ledger_path)
    planned = deploy_notional_usd(intent) or 0.0
    if intent.side == "BUY" and snapshot["unknown_fills"]:
        raise ExecutionError("unknown fill observed since budget_epoch; refusing this BUY only")
    if intent.side == "BUY" and snapshot["at_risk_position_value_usd"] + snapshot["pending_buy_reserve_usd"] + planned > total_cap + 1e-9:
        engage_global_halt("at-risk plus pending reserve cap would be breached", halt_file=halt_file)
        raise ExecutionError("at-risk principal plus pending reserve would exceed cap; HALTED")
    if intent.side == "BUY":
        flows = realized_flows
        if flows is None:
            flows = account_realized_flows(read_funder_address(secret_dir), since_utc=since or "")
        realized_loss = (
            (to_float(flows.get("buy_usd")) or 0.0)
            - (to_float(flows.get("sell_usd")) or 0.0)
            - (to_float(flows.get("redeem_usd")) or 0.0)
            - snapshot["at_risk_position_value_usd"]
        )
        snapshot["realized_flows"] = flows
        snapshot["realized_loss_usd"] = round(realized_loss, 8)
        snapshot["max_drawdown_usd_effective"] = drawdown_cap
        if realized_loss >= drawdown_cap - 1e-9:
            engage_global_halt(
                f"epoch drawdown fuse tripped (realized loss {realized_loss:.2f} >= {drawdown_cap:.2f})",
                halt_file=halt_file,
            )
            raise ExecutionError(
                "epoch realized loss reached max drawdown; BUY halted (exits unaffected)"
            )
    return snapshot


def cancel_order(client: Any, *, order_id: str) -> dict[str, Any]:
    """LIVE: cancel a resting order via the official SDK."""
    resp = client.cancel_order(order_id=order_id)
    return {
        "order_id": order_id,
        "canceled": [safe_str(x) for x in (attr(resp, "canceled") or [])],
        "not_canceled": attr(resp, "not_canceled"),
    }


# Operator enclave signing (optional). With the gate file present in the secret
# directory, the operator's own account signs through side-bound agents in the
# operator's own user-root sub-organization instead of the local key.
OWNER_TURNKEY_GATE_FILE = "OWNER_TURNKEY_ENABLED"
OWNER_TURNKEY_REFS = {
    "turnkey_organization_id": "turnkey_org_id.txt",
    "turnkey_signer_address": "turnkey_signer_address.txt",
    "turnkey_entry_agent_public_key": "turnkey_entry_agent_public_key.txt",
    "turnkey_entry_agent_private_key": "turnkey_entry_agent_private_key.txt",
    "turnkey_exit_agent_public_key": "turnkey_exit_agent_public_key.txt",
    "turnkey_exit_agent_private_key": "turnkey_exit_agent_private_key.txt",
}


def owner_turnkey_material(secret_dir: str) -> dict[str, str] | None:
    """Load the operator's enclave agent material, or None to stay on the local key.

    Opt-in twice over: every ref must exist AND the gate file must be present.
    This is the operator's own account and key, so removing the gate returns the
    next build to the operator's local key with no code change. Delegated
    mandates never come through here: they sign only through
    marketflow/guardian, with no local-key path at all.
    """
    if not os.path.exists(os.path.join(secret_dir, OWNER_TURNKEY_GATE_FILE)):
        return None
    material: dict[str, str] = {"key_backend": "turnkey_user_root"}
    for key, filename in OWNER_TURNKEY_REFS.items():
        path = os.path.join(secret_dir, filename)
        if not os.path.exists(path):
            # Gate on but material incomplete: refuse rather than silently signing
            # with the on-disk key the operator believed was retired.
            raise ExecutionError(
                f"{OWNER_TURNKEY_GATE_FILE} is present but {filename} is missing; "
                "refusing to fall back to the local private key"
            )
        material[key] = read_secret_ref(secret_dir, filename)
    return material


def build_secure_client(
    secrets: dict[str, str],
    *,
    secret_dir: str = DEFAULT_SECRET_DIR,
    side: str | None = None,
) -> Any:
    turnkey_material = owner_turnkey_material(secret_dir)
    if turnkey_material is not None:
        # Same wallet, same address, same Deposit Wallet -- only the custody of the
        # signing key changes. The agents' policies allow CLOB orders on one side
        # and nothing else, so a compromise of this box can trade the account but
        # cannot transfer out of it (marketflow/guardian/turnkey.py).
        from marketflow.guardian import turnkey as gturnkey

        selected_side = str(side or "").strip().upper()
        if selected_side not in {"BUY", "SELL"}:
            raise ExecutionError("operator enclave client requires an explicit BUY or SELL side")
        return gturnkey.build_turnkey_client(
            {**secrets, **turnkey_material},
            secret_dir=secret_dir,
            side=selected_side,
        )
    try:  # pragma: no cover - live SDK only.
        from polymarket import ApiKeyCreds, SecureClient
    except Exception as exc:
        raise ExecutionError(
            "polymarket-client SDK is unavailable in this Python environment; "
            "run with python3"
        ) from exc
    credentials = ApiKeyCreds(
        key=secrets["api_key"],
        secret=secrets["api_secret"],
        passphrase=secrets["passphrase"],
    )
    proxy_url = polymarket_sdk_proxy_url(secret_dir)
    with proxied_secure_client_transports(proxy_url):
        client = SecureClient.create(
            private_key=secrets["private_key"],
            wallet=secrets["funder_address"],
            credentials=credentials,
        )
    return client


def execute_order(
    client: Any,
    intent: OrderIntent,
    plan: dict[str, Any],
    *,
    expected_wallet_type: str = EXPECTED_WALLET_TYPE,
    ledger_path: str = DEFAULT_LEDGER,
    secret_dir: str = DEFAULT_SECRET_DIR,
    realized_flows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """LIVE: assert the plan was live-cleared and the wallet matches, then
    build+sign and submit.

    `realized_flows` is a SEALING seam for selftest only: the budget check
    otherwise reads the account's real flows, so account activity would silently
    change self-test outcomes — a drawdown in the account could trip the fuse inside
    an unrelated ledger-accounting check. Live callers never pass it.

    Only reached when plan["will_execute_live"] is True (the fuse gate already
    passed in plan_order). The wallet_type assertion here is the last brake
    immediately before signing.
    """
    if not plan.get("will_execute_live"):
        raise ExecutionError("execute_order called without a live-cleared plan; refusing")
    wallet = detect_wallet(client, expected_wallet_type)
    if not wallet["wallet_type_matches"]:
        raise ExecutionError(
            f"wallet_type {wallet['wallet_type']} != expected {expected_wallet_type}; refusing to sign"
        )
    account_budget = pre_live_account_budget_check(
        client, intent, plan, ledger_path=ledger_path, secret_dir=secret_dir,
        realized_flows=realized_flows,
    )
    signed = build_signed_order(client, intent)
    idem_key = plan.get("idempotency_key") or order_idempotency_key(intent)
    pending_id = f"pmxpending_{now_ms()}_{hash_obj(intent.redacted())[:10]}"
    pending_record = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "execution_id": pending_id,
        "idempotency_key": idem_key,
        "mode": LIVE_PENDING_MODE,
        "boundaries": EXECUTION_BOUNDARIES,
        "intent": intent.redacted(),
        "wallet": wallet,
        "account_budget_precheck": account_budget,
        "signed_order": redact_signed_order(signed),
        "ledger_write": {
            "pending_written": True,
            "live_executed_written": False,
            "ledger_path": ledger_path,
        },
    }
    append_jsonl(ledger_path, pending_record)
    receipt = redact_order_response(client.post_order(signed))
    fill = fill_from_receipt(receipt, intent)
    record = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "execution_id": f"pmxlive_{now_ms()}_{hash_obj(intent.redacted())[:10]}",
        "pending_execution_id": pending_id,
        "idempotency_key": idem_key,
        "mode": LIVE_EXECUTED_MODE,
        "boundaries": EXECUTION_BOUNDARIES,
        "intent": intent.redacted(),
        "wallet": wallet,
        "account_budget_precheck": account_budget,
        "signed_order": redact_signed_order(signed),
        "receipt": receipt,
        "fill": fill,
        "executed": bool(receipt.get("ok")),
        "ledger_write": {
            "pending_written": True,
            "live_executed_written": True,
            "ledger_path": ledger_path,
        },
    }
    try:
        append_jsonl(ledger_path, record)
    except Exception as exc:
        record["ledger_write"]["live_executed_written"] = False
        record["ledger_write"]["live_executed_write_error"] = f"{exc.__class__.__name__}: {exc}"
        try:
            engage_global_halt("LIVE_EXECUTED ledger append failed after post_order")
        except Exception:
            pass
    return record


def assert_no_secret_leak(row: dict[str, Any], secrets: dict[str, str]) -> list[str]:
    """Fail-loud guard: no raw secret value may appear anywhere in a ledger row."""
    blob = canonical_json(row)
    leaks = []
    for key, value in secrets.items():
        if value and len(value) >= 8 and value in blob:
            leaks.append(key)
    return leaks


def human_summary(record: dict[str, Any]) -> str:
    intent = record.get("intent", {})
    fuses = record.get("fuses", {})
    est = record.get("fill_estimate") or {}
    lines = [
        "# Polymarket Execution v0.1",
        "",
        f"- generated_at: `{record.get('generated_at')}`",
        f"- mode: `{record.get('mode')}`",
        f"- action / side: `{intent.get('action')}` / `{intent.get('side')}`",
        f"- order_kind: `{intent.get('order_kind')}`",
        f"- token_id: `{intent.get('token_id')}`",
        f"- will_execute_live: `{record.get('will_execute_live')}`",
        f"- would_place (fuses passed): `{record.get('would_place')}`",
        "",
        "## Fuses",
        "",
        f"- passed: `{fuses.get('passed')}`",
        f"- forced_dry_run: `{fuses.get('forced_dry_run')}`",
        f"- kill_switch_active: `{fuses.get('kill_switch_active')}`",
        f"- arm_valid: `{fuses.get('arm_valid')}`",
        f"- arm_mode: `{fuses.get('arm_mode')}`",
        f"- deploy_notional_usd: `{fuses.get('deploy_notional_usd')}`",
        f"- deployed_before_usd: `{fuses.get('deployed_before_usd')}`",
        f"- remaining_deploy_usd: `{fuses.get('remaining_deploy_usd')}`",
        f"- refusals: `{fuses.get('refusals')}`",
        "",
        "## Fill estimate (read-only)",
        "",
        f"- basis: `{est.get('basis')}`",
        f"- average_price: `{est.get('average_price')}`",
        f"- estimated_cost_usd: `{est.get('estimated_cost_usd')}`",
        f"- estimated_proceeds_usd: `{est.get('estimated_proceeds_usd')}`",
        f"- full_fill: `{est.get('full_fill')}`",
        "",
        "## Boundary",
        "",
        "Dry_run by default. Live requires an armed arm-state (mode permits side) + caps + no kill switch. "
        "Exit-first; no naked short; no secret printed or ledgered; "
        "no coupling to any other runtime's state.",
        "",
    ]
    return "\n".join(lines)


def record_and_write(record: dict[str, Any], args: argparse.Namespace, *, secrets: dict[str, str] | None = None) -> None:
    if secrets:
        leaks = assert_no_secret_leak(record, secrets)
        if leaks:
            if record.get("mode") == LIVE_EXECUTED_MODE:
                engage_global_halt("secret leak guard tripped after live order submission")
            raise ExecutionError(f"secret leak guard tripped for: {', '.join(sorted(leaks))}")
    ledger_write = record.get("ledger_write") if isinstance(record.get("ledger_write"), dict) else {}
    already_written = record.get("mode") == LIVE_EXECUTED_MODE and ledger_write.get("live_executed_written") is True
    if not already_written:
        if record.get("mode") == LIVE_EXECUTED_MODE:
            ledger_write = dict(ledger_write)
            ledger_write["live_executed_written"] = True
            record = dict(record)
            record["ledger_write"] = ledger_write
        append_jsonl(args.ledger, record)
    write_json(args.latest_json, record)
    ensure_parent(args.latest_summary)
    with open(args.latest_summary, "w", encoding="utf-8") as f:
        f.write(human_summary(record))


def fetch_book_readonly(token_id: str) -> Any:
    """Read-only public order book for the dry_run fill estimate (no auth, no SDK orders)."""
    import urllib.request

    url = "https://clob.polymarket.com/book?token_id=" + token_id
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_books_readonly(token_ids: list[str]) -> Any:
    """Read-only public batch order books. No authentication or order capability."""
    import urllib.request

    if not token_ids:
        return []
    if len(token_ids) > 500:
        raise ValueError("Polymarket POST /books accepts at most 500 token IDs")
    url = "https://clob.polymarket.com/books"
    body = json.dumps([{"token_id": str(token_id)} for token_id in token_ids]).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(args: argparse.Namespace) -> int:
    if not args.config:
        print("error: --config is required (an order-intent JSON) unless --selftest is used", file=sys.stderr)
        return 2
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        print("error: order-intent config must be a JSON object", file=sys.stderr)
        return 2

    intent = intent_from_config(config)
    # Operator-path callers must build caps the same way the daemon's operator path
    # does (owner_fuse_caps), not from bare FuseCaps: the bare defaults sit BELOW the
    # operator ceiling, so an arm-state whose operational caps exceed them would be
    # judged cap_tamper -> arm void -> even exits frozen.
    caps = owner_fuse_caps(
        max_total_deploy_usd=args.max_total_deploy_usd,
        max_per_trade_usd=args.max_per_trade_usd,
    )

    book = None
    if args.estimate_fill:
        try:
            book = fetch_book_readonly(intent.token_id)
        except Exception as exc:
            print(f"warning: read-only fill estimate skipped: {exc}", file=sys.stderr)

    plan = plan_order(
        intent,
        requested_live=args.live,
        caps=caps,
        kill_file=args.kill_file,
        arm_state_file=args.arm_state_file,
        ledger_path=args.ledger,
        book=book,
    )

    secrets = None
    record = plan
    if plan["will_execute_live"]:
        # Live path is gated and never reached by selftest/unattended runs. It is
        # only reached when --live + an armed arm-state (mode permits side) + caps
        # + no kill switch all hold. This session does not exercise it.
        secrets = load_polymarket_secret_refs(args.secret_dir)
        client = build_secure_client(secrets, secret_dir=args.secret_dir, side=intent.side)
        try:
            record = execute_order(
                client,
                intent,
                plan,
                expected_wallet_type=args.expected_wallet_type,
                ledger_path=args.ledger,
            )
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - best-effort close.
                pass

    record_and_write(record, args, secrets=secrets)

    if args.print_json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{record['generated_at']} mode={record['mode']} "
            f"side={intent.side} would_place={plan.get('would_place')} "
            f"will_execute_live={plan.get('will_execute_live')}"
        )
    if plan["will_execute_live"]:
        return 0 if record.get("executed") else 2
    return 0 if plan.get("would_place") else 2


def engage_kill(kill_file: str) -> int:
    ensure_parent(kill_file)
    with open(kill_file, "w", encoding="utf-8") as f:
        f.write(f"engaged {iso_now()}\n")
    print(f"kill switch engaged: {kill_file}")
    return 0


# ---------------------------------------------------------------------------
# Selftest: synthetic fixtures only. No network, no SDK, no secrets.
# ---------------------------------------------------------------------------


class _FakeBook:
    def __init__(self, bids, asks):
        self.bids = [type("L", (), {"price": Decimal(str(p)), "size": Decimal(str(s))})() for p, s in bids]
        self.asks = [type("L", (), {"price": Decimal(str(p)), "size": Decimal(str(s))})() for p, s in asks]


class _FakeSignedOrder:
    token_id = "111222333"
    side = "SELL"
    order_type = "FOK"
    signature_type = 1
    maker_amount = Decimal("5000000")
    taker_amount = Decimal("10000000")
    expiration = 0
    post_only = False
    signer = "0x" + "a" * 40
    maker = "0x" + "b" * 40
    signature = "0x" + "c" * 130


class _FakeAccepted:
    ok = True
    order_id = "ord-live-1"
    status = "matched"
    making_amount = Decimal("1.0")
    taking_amount = Decimal("2.0")
    trade_ids = ("trd-1",)
    transactions_hashes = ("0x" + "e" * 64,)


class _FakeRejected:
    ok = False
    code = "fok_not_filled"
    message = "fill-or-kill not filled"


class _FakeCancelResp:
    canceled = ("ord-live-1",)
    not_canceled = {}


class _FakePage:
    def __init__(self, items):
        self.items = items
        self.has_more = False


class _FakeTrade:
    id = "trd-1"
    taker_order_id = "ord-live-1"
    side = "BUY"
    price = Decimal("1.0")
    size = Decimal("1.0")
    matched_at = "2026-01-01T00:00:00Z"
    transaction_hash = "0x" + "e" * 64
    maker_orders = ()


class _FakeMakerOrder:
    def __init__(self, order_id: str):
        self.order_id = order_id


class _FakePosition:
    current_value = Decimal("1.0")
    initial_value = Decimal("1.0")


class _FakeClient:
    """Records SDK calls so the selftest can prove dry_run never signs/submits."""

    def __init__(self, *, wallet_type="DEPOSIT_WALLET", reject=False, trades=None, orders=None, positions=None):
        self.wallet_type = wallet_type
        self.wallet = "0x" + "f" * 40
        self.calls: list[str] = []
        self._reject = reject
        self._trades = trades or []
        self._orders = orders or []
        self._positions = positions or []

    def create_market_order(self, **kwargs):
        self.calls.append("create_market_order")
        return _FakeSignedOrder()

    def create_limit_order(self, **kwargs):
        self.calls.append("create_limit_order")
        return _FakeSignedOrder()

    def post_order(self, signed):
        self.calls.append("post_order")
        return _FakeRejected() if self._reject else _FakeAccepted()

    def cancel_order(self, *, order_id):
        self.calls.append("cancel_order")
        return _FakeCancelResp()

    def list_account_trades(self, *args, **kwargs):
        self.calls.append("list_account_trades")
        return iter([_FakePage(self._trades)])

    def list_open_orders(self, *args, **kwargs):
        self.calls.append("list_open_orders")
        return iter([_FakePage(self._orders)])

    def list_positions(self, *args, **kwargs):
        self.calls.append("list_positions")
        return iter([_FakePage(self._positions)])

    def close(self):
        self.calls.append("close")


def selftest() -> dict[str, Any]:
    global DEFAULT_GLOBAL_HALT_FILE
    checks: dict[str, bool] = {}
    # Seal the account-flow read. Without this, execute_order's budget check would
    # query a real account's data-api flows, and unrelated selftest outcomes (the
    # ledger-accounting check, for one) would depend on whatever that account did
    # that day. Tests that deliberately exercise drawdown/budget pass their own
    # flows to pre_live_account_budget_check directly.
    _SEALED_FLOWS = {"buy_usd": 0.0, "sell_usd": 0.0, "redeem_usd": 0.0}
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp_ledger = os.path.join(OUT_DIR, "selftest_ledger.jsonl")
    tmp_kill = os.path.join(OUT_DIR, "selftest_kill_DOES_NOT_EXIST")
    tmp_ack_missing = os.path.join(OUT_DIR, "selftest_arm_missing.json")
    tmp_ack_valid = os.path.join(OUT_DIR, "selftest_arm_state.json")
    saved_global_halt = DEFAULT_GLOBAL_HALT_FILE
    tmp_global_halt = os.path.join(OUT_DIR, "selftest_GLOBAL_HALT_DOES_NOT_EXIST")
    DEFAULT_GLOBAL_HALT_FILE = tmp_global_halt
    for path in (tmp_ledger, tmp_kill, tmp_ack_missing, tmp_ack_valid, tmp_global_halt):
        if os.path.exists(path):
            os.remove(path)
    caps = FuseCaps()

    def _write_arm(path, *, armed=True, mode="full", phrase=LIVE_ACK_PHRASE,
                   writer=ARM_WRITER, total=None, per_trade=None, expires_at=None):
        obj = {
            "schema_version": ARM_STATE_SCHEMA_VERSION,
            "armed": armed,
            "mode": mode,
            "live_ack": phrase,
            "written_by": writer,
            "budget_epoch": "selftest-epoch",
            "budget_epoch_started_at": "2026-01-02T00:00:00Z",
        }
        if total is not None:
            obj["max_total_deploy_usd"] = total
        if per_trade is not None:
            obj["max_per_trade_usd"] = per_trade
        if expires_at is not None:
            obj["expires_at"] = expires_at
        write_json(path, obj)

    # allowlist / blacklist disjoint
    used = set(
        ALLOWED_SDK_BUILD_METHODS
        + ALLOWED_SDK_SUBMIT_METHODS
        + ALLOWED_SDK_CANCEL_METHODS
        + ALLOWED_SDK_READBACK_METHODS
        + ALLOWED_SDK_QUOTE_METHODS
    )
    checks["forbidden_methods_not_used"] = not (used & set(FORBIDDEN_SDK_METHODS))
    checks["place_methods_forbidden"] = (
        "place_limit_order" in FORBIDDEN_SDK_METHODS and "place_market_order" in FORBIDDEN_SDK_METHODS
    )

    # Operator enclave signing: the gate replaces (rather than supplements) the raw
    # key, requires both side-specific agents, and fails closed on partial material.
    owner_secret_test_dir = tempfile.mkdtemp(prefix="owner-turnkey-selftest-", dir=OUT_DIR)
    try:
        base_refs = {
            "polymarket_api_key.txt": "selftest-api-key",
            "polymarket_api_secret.txt": "selftest-api-secret",
            "polymarket_passphrase.txt": "selftest-passphrase",
            "polymarket_funder_address.txt": "0x" + "a" * 40,
            "turnkey_org_id.txt": "selftest-org",
            "turnkey_signer_address.txt": "0x" + "b" * 40,
            "turnkey_entry_agent_public_key.txt": "entry-public",
            "turnkey_entry_agent_private_key.txt": "entry-private",
            "turnkey_exit_agent_public_key.txt": "exit-public",
            "turnkey_exit_agent_private_key.txt": "exit-private",
        }
        for filename, value in base_refs.items():
            with open(os.path.join(owner_secret_test_dir, filename), "w", encoding="utf-8") as f:
                f.write(value)
        open(os.path.join(owner_secret_test_dir, OWNER_TURNKEY_GATE_FILE), "a", encoding="utf-8").close()
        owner_loaded = load_polymarket_secret_refs(owner_secret_test_dir)
        owner_material = owner_turnkey_material(owner_secret_test_dir) or {}
        checks["owner_turnkey_user_root_needs_no_raw_key"] = "private_key" not in owner_loaded
        checks["owner_turnkey_user_root_has_separate_agents"] = (
            owner_material.get("key_backend") == "turnkey_user_root"
            and owner_material.get("turnkey_entry_agent_private_key") == "entry-private"
            and owner_material.get("turnkey_exit_agent_private_key") == "exit-private"
        )
        # The enclave branch resolves its signer module and demands a side before
        # anything touches the SDK or the network.
        try:
            build_secure_client(owner_loaded, secret_dir=owner_secret_test_dir, side=None)
            checks["owner_turnkey_client_requires_side"] = False
        except ExecutionError as exc:
            checks["owner_turnkey_client_requires_side"] = "explicit BUY or SELL side" in str(exc)
        except Exception:  # noqa: BLE001 - an import failure here is the bug this pins
            checks["owner_turnkey_client_requires_side"] = False
        os.remove(os.path.join(owner_secret_test_dir, "turnkey_exit_agent_private_key.txt"))
        try:
            load_polymarket_secret_refs(owner_secret_test_dir)
            checks["owner_turnkey_partial_material_fails_closed"] = False
        except ExecutionError:
            checks["owner_turnkey_partial_material_fails_closed"] = True
    finally:
        shutil.rmtree(owner_secret_test_dir)

    # Delegated-mandate caps stay bound: a cap above the module default clamps
    # DOWN to the default regardless of caller input. Sized off the constants so
    # the assertion holds at any book size.
    raised = FuseCaps(max_total_deploy_usd=DEFAULT_MAX_TOTAL_DEPLOY_USD * 10,
                      max_per_trade_usd=DEFAULT_MAX_PER_TRADE_USD * 10)
    checks["caps_weld_clamps_down"] = (
        raised.max_total_deploy_usd == DEFAULT_MAX_TOTAL_DEPLOY_USD
        and raised.max_per_trade_usd == DEFAULT_MAX_PER_TRADE_USD
    )
    _tight_total = DEFAULT_MAX_TOTAL_DEPLOY_USD * 0.4
    _tight_per_trade = DEFAULT_MAX_PER_TRADE_USD * 0.1
    tighter = FuseCaps(max_total_deploy_usd=_tight_total, max_per_trade_usd=_tight_per_trade)
    checks["caps_weld_allows_tighten"] = (
        tighter.max_total_deploy_usd == _tight_total and tighter.max_per_trade_usd == _tight_per_trade
    )
    # Operator caps (single-account) may be RAISED above the default, up to
    # the fat-finger ceiling; a stray extra zero past the ceiling still clamps.
    _raise_total = DEFAULT_MAX_TOTAL_DEPLOY_USD * 2
    _raise_per_trade = DEFAULT_MAX_PER_TRADE_USD * 2
    owner_raised = owner_fuse_caps(max_total_deploy_usd=_raise_total, max_per_trade_usd=_raise_per_trade)
    checks["owner_caps_can_raise_above_default"] = (
        owner_raised.max_total_deploy_usd == _raise_total
        and owner_raised.max_per_trade_usd == _raise_per_trade
        and _raise_total <= OWNER_CAP_CEILING_TOTAL_USD
        and _raise_per_trade <= OWNER_CAP_CEILING_PER_TRADE_USD
    )
    owner_fat_finger = owner_fuse_caps(max_total_deploy_usd=1.0e9, max_per_trade_usd=1.0e9)
    checks["owner_caps_clamp_at_fat_finger_ceiling"] = (
        owner_fat_finger.max_total_deploy_usd == OWNER_CAP_CEILING_TOTAL_USD
        and owner_fat_finger.max_per_trade_usd == OWNER_CAP_CEILING_PER_TRADE_USD
    )
    # Non-custodial invariant: the principal's ceiling must NOT leak into the
    # default — a plain FuseCaps(), which is what every delegated mandate gets on
    # this path, is unaffected by it.
    tenant_still_bound = FuseCaps(max_total_deploy_usd=OWNER_CAP_CEILING_TOTAL_USD * 10)
    checks["tenant_default_still_bound"] = (
        tenant_still_bound.max_total_deploy_usd == DEFAULT_MAX_TOTAL_DEPLOY_USD
    )

    # mask
    checks["mask_keeps_head_tail"] = mask_address("0x" + "a" * 36 + "bcde") == "0xaaaa…bcde"

    # Polymarket SDK proxy config: only dedicated config sources are honored.
    saved_proxy_env = {
        "MARKETFLOW_POLYMARKET_PROXY_URL": os.environ.get("MARKETFLOW_POLYMARKET_PROXY_URL"),
        "POLYMARKET_HTTP_PROXY": os.environ.get("POLYMARKET_HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "https_proxy": os.environ.get("https_proxy"),
        "http_proxy": os.environ.get("http_proxy"),
    }
    try:
        os.environ.pop("MARKETFLOW_POLYMARKET_PROXY_URL", None)
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.pop(name, None)
        os.environ["POLYMARKET_HTTP_PROXY"] = "http://user:secret@legacy-proxy.example:8080"
        checks["polymarket_http_proxy_env_recognized"] = (
            polymarket_sdk_proxy_url(None) == "http://user:secret@legacy-proxy.example:8080"
        )

        os.environ["MARKETFLOW_POLYMARKET_PROXY_URL"] = "http://marketflow-proxy.example:1111"
        checks["marketflow_proxy_env_keeps_priority"] = (
            polymarket_sdk_proxy_url(None) == "http://marketflow-proxy.example:1111"
        )

        os.environ.pop("MARKETFLOW_POLYMARKET_PROXY_URL", None)
        os.environ.pop("POLYMARKET_HTTP_PROXY", None)
        os.environ["http_proxy"] = "http://global-proxy.example:9999"
        # A process-wide proxy variable is ignored, and with nothing configured
        # for the venue SDK the connection is direct.
        checks["global_proxy_env_ignored_default_direct"] = (
            polymarket_sdk_proxy_url(None) is None
        )
    finally:
        for name, value in saved_proxy_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # Fuse 3: no arm-state file -> forced dry_run even when --live requested
    exit_intent = build_exit_intent(token_id="111222333", held_shares=10.0, order_kind="market", min_price=0.01)
    plan_no_ack = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_missing, ledger_path=tmp_ledger,
    )
    checks["no_arm_state_forces_dry_run"] = plan_no_ack["mode"] == "DRY_RUN_PLAN" and plan_no_ack["will_execute_live"] is False
    checks["exit_passes_fuses_dry_run"] = plan_no_ack["would_place"] is True

    # valid armed arm-state (mode full) -> live plan clears (still dry_run if not requested_live)
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    plan_live = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["valid_ack_allows_live_plan"] = plan_live["will_execute_live"] is True
    plan_dry_default = plan_order(
        exit_intent, requested_live=False, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["default_is_dry_run"] = plan_dry_default["will_execute_live"] is False

    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    with open(tmp_ack_valid, encoding="utf-8") as f:
        no_epoch = json.load(f)
    no_epoch.pop("budget_epoch", None)
    write_json(tmp_ack_valid, no_epoch)
    plan_no_epoch = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["budget_epoch_required_for_live"] = plan_no_epoch["will_execute_live"] is False
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # Fuse 2: kill switch (file) forces dry_run even with valid ack + --live
    ensure_parent(tmp_kill)
    with open(tmp_kill, "w", encoding="utf-8") as f:
        f.write("engaged\n")
    plan_killed = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["kill_switch_forces_dry_run"] = (
        plan_killed["will_execute_live"] is False and plan_killed["fuses"]["kill_switch_active"] is True
    )
    os.remove(tmp_kill)

    # Fuse 2 (env): env kill var also forces dry_run
    os.environ[KILL_SWITCH_ENV] = "1"
    plan_env_kill = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["env_kill_forces_dry_run"] = plan_env_kill["will_execute_live"] is False
    del os.environ[KILL_SWITCH_ENV]

    # Fuse 1: per-trade cap (BUY). Sized off the cap under test so the assertion
    # is about the fuse, not about a particular book size.
    _over_cap_shares = (caps.max_per_trade_usd / 0.5) * 2.0
    big_buy = build_entry_intent(token_id="111222333", order_kind="limit",
                                 price=0.5, size=_over_cap_shares)
    plan_big = plan_order(
        big_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["per_trade_cap_blocks_buy"] = plan_big["would_place"] is False and any(
        "per-trade cap" in r for r in plan_big["fuses"]["refusals"]
    )

    small_buy = build_entry_intent(token_id="111222333", order_kind="limit", price=0.5, size=2.0)
    plan_small = plan_order(
        small_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["small_buy_passes"] = plan_small["would_place"] is True and plan_small["will_execute_live"] is True
    checks["small_buy_is_post_only_maker"] = (
        plan_small["intent"]["post_only"] is True
        and plan_small["execution_quality"]["maker_candidate"] is True
    )

    market_buy_no_speed = build_entry_intent(token_id="111222333", order_kind="market", max_spend_usd=1.0, max_price=0.5)
    plan_market_no_speed = plan_order(
        market_buy_no_speed, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["market_buy_without_speed_rejected"] = plan_market_no_speed["would_place"] is False and any(
        "in_play_speed_window" in r for r in plan_market_no_speed["fuses"]["refusals"]
    )
    market_buy_speed = build_entry_intent(
        token_id="111222333",
        order_kind="market",
        max_spend_usd=1.0,
        max_price=0.5,
        taker_allowed_reason="in_play_speed_window",
    )
    plan_market_speed = plan_order(
        market_buy_speed, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["market_buy_speed_window_allowed"] = plan_market_speed["would_place"] is True

    # Fuse 1: total deploy cap via ack tightening.
    _write_arm(tmp_ack_valid, mode="full", total=0.5)
    plan_tight = plan_order(
        small_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["ack_budget_tightens_total_cap"] = plan_tight["would_place"] is False and any(
        "total deploy cap" in r for r in plan_tight["fuses"]["refusals"]
    )
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # Fuse 4: no naked short (sell more than held)
    over_sell = OrderIntent(action="EXIT", side="SELL", token_id="x", order_kind="market", size=20.0, held_shares=10.0, min_price=0.01)
    plan_over = plan_order(
        over_sell, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["naked_short_refused"] = plan_over["would_place"] is False and any(
        "naked short" in r for r in plan_over["fuses"]["refusals"]
    )

    # longshot limit price rejected
    longshot = build_entry_intent(token_id="x", order_kind="limit", price=0.005, size=2.0)
    plan_long = plan_order(
        longshot, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["longshot_price_rejected"] = plan_long["would_place"] is False

    # --- BUY price floor --------------------------------
    # It must bind on BOTH order shapes: the daemon opens limit/post-only, and
    # Guardian user buys are market FOK — the shape that had no price gate at all.
    def _floor_plan(intent, enforce: bool) -> dict:
        global BUY_PRICE_FLOOR_ENFORCE
        prior, BUY_PRICE_FLOOR_ENFORCE = BUY_PRICE_FLOOR_ENFORCE, enforce
        try:
            return plan_order(intent, requested_live=True, caps=caps, kill_file=tmp_kill,
                              arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger)
        finally:
            BUY_PRICE_FLOOR_ENFORCE = prior

    sub10_limit = build_entry_intent(token_id="x", order_kind="limit", price=0.06, size=20.0)
    p_shadow = _floor_plan(sub10_limit, False)
    p_live = _floor_plan(sub10_limit, True)
    checks["floor_shadow_computes_without_refusing"] = (
        p_shadow["fuses"]["checks"]["buy_price_floor"]["breached"] is True
        and not any("price floor" in r for r in p_shadow["fuses"]["refusals"])
    )
    checks["floor_enforced_refuses_sub10c_limit"] = (
        p_live["would_place"] is False
        and any("price floor" in r for r in p_live["fuses"]["refusals"])
    )
    sub10_market = build_entry_intent(
        token_id="x", order_kind="market", max_spend_usd=2.0, max_price=0.06,
        taker_allowed_reason="in_play_speed_window")
    p_mkt = _floor_plan(sub10_market, True)
    checks["floor_enforced_refuses_sub10c_market_buy"] = (
        p_mkt["would_place"] is False
        and p_mkt["fuses"]["checks"]["buy_price_floor"]["basis"] == "max_price"
    )
    at_floor = build_entry_intent(token_id="x", order_kind="limit", price=0.10, size=20.0)
    checks["floor_boundary_10c_admitted"] = (
        _floor_plan(at_floor, True)["fuses"]["checks"]["buy_price_floor"]["breached"] is False
    )
    override = build_entry_intent(token_id="x", order_kind="limit", price=0.06, size=20.0)
    override.price_floor_override_reason = "owner: negRisk complement leg"
    p_ovr = _floor_plan(override, True)
    checks["floor_override_admits_and_audits"] = (
        not any("price floor" in r for r in p_ovr["fuses"]["refusals"])
        and p_ovr["fuses"]["checks"]["buy_price_floor"]["override_reason"] == "owner: negRisk complement leg"
        and p_ovr["intent"]["price_floor_override_reason"] == "owner: negRisk complement leg"
    )
    cheap_exit = build_exit_intent(token_id="x", held_shares=20.0, order_kind="limit", price=0.06, size=20.0)
    p_exit = _floor_plan(cheap_exit, True)
    checks["floor_never_blocks_an_exit"] = (
        "buy_price_floor" not in p_exit["fuses"]["checks"]
        and not any("price floor" in r for r in p_exit["fuses"]["refusals"])
    )

    unbounded_sell = build_exit_intent(token_id="x", held_shares=5.0, order_kind="market")
    plan_unbounded_sell = plan_order(
        unbounded_sell, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["unbounded_market_sell_rejected"] = plan_unbounded_sell["would_place"] is False and any(
        "unbounded" in r for r in plan_unbounded_sell["fuses"]["refusals"]
    )

    # Fuse 3 (mode-permits): exit_only allows SELL but NOT BUY; full allows BUY
    _write_arm(tmp_ack_valid, mode="exit_only", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    sell_exit_only = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    buy_exit_only = plan_order(
        small_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["exit_only_allows_sell"] = sell_exit_only["will_execute_live"] is True
    checks["exit_only_blocks_buy"] = buy_exit_only["will_execute_live"] is False
    # delegated-mandate entry mode: permits both sides like full, but is a
    # distinct named capability. An unknown/garbage mode must still fail closed.
    _write_arm(tmp_ack_valid, mode="entry_allowlisted", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    checks["entry_allowlisted_allows_buy"] = plan_order(
        small_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )["will_execute_live"] is True
    checks["entry_allowlisted_allows_sell"] = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )["will_execute_live"] is True
    _write_arm(tmp_ack_valid, mode="entry_anything", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    checks["unknown_arm_mode_fails_closed"] = plan_order(
        small_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )["will_execute_live"] is False
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # arm-state written_by != operator (for example self-armed by software) -> refused
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, writer="marketflow")
    self_armed = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["self_armed_refused"] = self_armed["will_execute_live"] is False and self_armed["fuses"]["arm_valid"] is False

    # Guardian mandate arm semantics: per-tenant writer/
    # ack/tenant expectations. A guardian-written arm file must NOT arm the
    # default (operator) path, and vice versa; tenant_id binds the file to its dir.
    g_ack = "GUARDIAN_TENANT_APPROVES_AUTO_EXIT"
    write_json(tmp_ack_valid, {
        "schema_version": ARM_STATE_SCHEMA_VERSION, "armed": True, "mode": "exit_only",
        "live_ack": g_ack, "written_by": "guardian_service", "tenant_id": "t1",
        "budget_epoch": "g-epoch", "budget_epoch_started_at": "2026-01-02T00:00:00Z",
    })
    g_on_default = load_arm_state(tmp_ack_valid, caps)
    checks["guardian_arm_never_arms_operator_path"] = g_on_default["valid"] is False
    g_ok = load_arm_state(tmp_ack_valid, caps, expected_writer="guardian_service",
                          expected_ack=g_ack, expected_tenant="t1")
    checks["guardian_arm_valid_with_expectations"] = g_ok["valid"] is True and g_ok["mode"] == "exit_only"
    g_wrong_tenant = load_arm_state(tmp_ack_valid, caps, expected_writer="guardian_service",
                                    expected_ack=g_ack, expected_tenant="t2")
    checks["guardian_arm_tenant_bound"] = g_wrong_tenant["valid"] is False
    g_sell = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
        arm_expected_writer="guardian_service", arm_expected_ack=g_ack, arm_expected_tenant="t1",
    )
    checks["guardian_exit_only_sell_live_plans"] = g_sell["will_execute_live"] is True
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    operator_on_guardian = load_arm_state(tmp_ack_valid, caps, expected_writer="guardian_service",
                                        expected_ack=g_ack, expected_tenant="t1")
    checks["operator_arm_never_arms_guardian_tenant"] = operator_on_guardian["valid"] is False

    # cap-tamper: a cap that EXCEEDS the code cap force-disarms (caps only go down)
    _write_arm(tmp_ack_valid, mode="full", total=1000000.0)
    tampered = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["cap_tamper_disarms"] = (
        tampered["will_execute_live"] is False
        and tampered["fuses"]["checks"]["arm_state"]["cap_tamper"] is True
    )
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # mode "off" / disarmed -> dry_run
    _write_arm(tmp_ack_valid, armed=False, mode="off")
    disarmed = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["disarmed_forces_dry_run"] = disarmed["will_execute_live"] is False
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # Expiry semantics: expired downgrades to exit_only rather than stopping
    # everything, because stopping SELL turns an open position into an unmanaged
    # one. Backward compatibility is a hard requirement: a file without
    # expires_at must behave bit-for-bit as before.
    _past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    _future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)          # no expires_at
    _no_exp = load_arm_state(tmp_ack_valid, caps)
    checks["no_expiry_field_behaves_exactly_as_before"] = (
        _no_exp["valid"] is True and _no_exp["mode"] == "full"
        and _no_exp["expired_downgraded"] is False)

    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, expires_at=_future)
    _fresh = load_arm_state(tmp_ack_valid, caps)
    checks["unexpired_authorisation_keeps_full"] = (
        _fresh["valid"] is True and _fresh["mode"] == "full"
        and _fresh["expired_downgraded"] is False)

    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, expires_at=_past)
    _exp = load_arm_state(tmp_ack_valid, caps)
    checks["expired_downgrades_to_exit_only"] = (
        _exp["valid"] is True and _exp["mode"] == "exit_only"
        and _exp["expired_downgraded"] is True
        and _exp.get("mode_before_expiry") == "full")
    checks["expired_refuses_buy"] = arm_mode_permits(_exp["mode"], "BUY") is False
    checks["expired_still_allows_sell"] = arm_mode_permits(_exp["mode"], "SELL") is True

    # The downgrade only happens when everything else holds: tampering or a wrong
    # writer still stops completely. Fail-closed is not weakened by it.
    _write_arm(tmp_ack_valid, mode="full", total=1000000.0, expires_at=_past)
    _exp_tamper = load_arm_state(tmp_ack_valid, caps)
    checks["expired_plus_tamper_still_fully_disarms"] = (
        _exp_tamper["valid"] is False and _exp_tamper["mode"] == "off"
        and _exp_tamper["expired_downgraded"] is False)

    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, writer="not_bridge", expires_at=_past)
    _exp_writer = load_arm_state(tmp_ack_valid, caps)
    checks["expired_plus_wrong_writer_still_fully_disarms"] = _exp_writer["valid"] is False

    # An unparseable expiry is still treated as expired (fail closed), but takes
    # the downgrade rather than the full stop, so exits survive.
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, expires_at="not-a-timestamp")
    _bad_exp = load_arm_state(tmp_ack_valid, caps)
    checks["unparseable_expiry_downgrades_not_arms_entry"] = (
        _bad_exp["mode"] == "exit_only" and _bad_exp["expired_downgraded"] is True)

    # Under an expired arm a BUY plan must actually be refused, checked
    # end-to-end through plan_order rather than only at load_arm_state.
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD, expires_at=_past)
    _ttl_buy_intent = build_entry_intent(
        token_id="111222333", order_kind="limit", price=0.5, size=2.0)
    _exp_buy_plan = plan_order(
        _ttl_buy_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    _exp_sell_plan = plan_order(
        exit_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["expired_buy_plan_refused_end_to_end"] = _exp_buy_plan["will_execute_live"] is False
    checks["expired_sell_plan_still_live_end_to_end"] = _exp_sell_plan["will_execute_live"] is True
    _write_arm(tmp_ack_valid, mode="full", total=DEFAULT_MAX_TOTAL_DEPLOY_USD)

    # read-only fill estimate: SELL sweeps bids
    book = _FakeBook(bids=[(0.60, 6.0), (0.58, 6.0)], asks=[(0.63, 5.0)])
    est = estimate_fill(build_exit_intent(token_id="x", held_shares=10.0, order_kind="market", min_price=0.01), book)
    checks["sell_estimate_full_fill"] = est["full_fill"] is True
    checks["sell_estimate_proceeds"] = abs(est["estimated_proceeds_usd"] - (6 * 0.60 + 4 * 0.58)) < 1e-9

    # market BUY estimate respects spend budget
    buy_est = estimate_fill(
        build_entry_intent(token_id="x", order_kind="market", max_spend_usd=1.0, max_price=0.7), book
    )
    checks["buy_estimate_within_budget"] = buy_est["estimated_cost_usd"] <= 1.0 + 1e-9
    maker_quality = execution_quality_estimate(build_entry_intent(token_id="x", order_kind="limit", price=0.62, size=2.0), book)
    checks["execution_quality_estimates_spread_saved"] = (
        maker_quality["maker_candidate"] is True
        and abs((maker_quality["estimated_spread_saved_usd"] or 0.0) - 0.02) < 1e-9
    )

    # wallet detection / mismatch refusal
    good_client = _FakeClient(wallet_type="DEPOSIT_WALLET")
    bad_client = _FakeClient(wallet_type="EOA")
    checks["wallet_matches"] = detect_wallet(good_client, EXPECTED_WALLET_TYPE)["wallet_type_matches"] is True
    checks["wallet_mismatch_detected"] = detect_wallet(bad_client, EXPECTED_WALLET_TYPE)["wallet_type_matches"] is False

    # execute_order refuses a non-live-cleared plan
    refused = False
    try:
        execute_order(good_client, exit_intent, {"will_execute_live": False})
    except ExecutionError:
        refused = True
    checks["execute_refuses_dry_plan"] = refused

    # live execute path (fake client): builds market order, posts, never uses place_*
    live_client = _FakeClient(wallet_type="DEPOSIT_WALLET")
    rec = execute_order(live_client, exit_intent, plan_live, expected_wallet_type="DEPOSIT_WALLET", ledger_path=tmp_ledger, realized_flows=_SEALED_FLOWS)
    checks["live_execute_reads_account_before_sign"] = live_client.calls[:3] == [
        "list_account_trades", "list_open_orders", "list_positions"
    ]
    checks["live_execute_signs_then_posts"] = live_client.calls[-2:] == ["create_market_order", "post_order"]
    checks["live_execute_no_place_calls"] = not any(c.startswith("place_") for c in live_client.calls)
    checks["live_execute_records_fill"] = rec["executed"] is True and rec["fill"]["filled_notional_usd"] == 2.0
    # A resting post-only maker order is accepted, not filled. Conflating the two
    # made every fill rate computed off this ledger read high (see fill_from_receipt).
    _resting_intent = OrderIntent(action="ENTER", token_id="t", side="BUY", size=5.0,
                                  price=0.80, order_kind="limit", post_only=True)
    _resting = fill_from_receipt(
        {"ok": True, "status": "live", "making_amount": 0.0, "taking_amount": 0.0,
         "trade_ids": [], "order_id": "0xrest"}, _resting_intent)
    checks["resting_maker_order_is_not_a_fill"] = (
        _resting["filled"] is False and _resting["resting"] is True
        and _resting["accepted"] is True and _resting["filled_shares"] == 0.0)
    _matched = fill_from_receipt(
        {"ok": True, "status": "matched", "making_amount": 4.0, "taking_amount": 5.0,
         "trade_ids": ["t1"], "order_id": "0xm"}, _resting_intent)
    checks["matched_order_is_a_fill"] = (
        _matched["filled"] is True and _matched["resting"] is False
        and _matched["filled_notional_usd"] == 4.0 and _matched["average_price"] == 0.8)
    _rejected = fill_from_receipt({"ok": False, "message": "rejected"}, _resting_intent)
    checks["rejected_order_is_neither_filled_nor_resting"] = (
        _rejected["filled"] is False and _rejected["resting"] is False
        and _rejected["accepted"] is False)
    checks["signed_order_redacted"] = (
        rec["signed_order"]["signer_masked"].startswith("0xaaaa")
        and "signature" not in rec["signed_order"]
        and ("c" * 130) not in canonical_json(rec)
    )

    # wallet mismatch blocks signing
    blocked = False
    bad_live = _FakeClient(wallet_type="EOA")
    try:
        execute_order(bad_live, exit_intent, plan_live, expected_wallet_type="DEPOSIT_WALLET", ledger_path=tmp_ledger, realized_flows=_SEALED_FLOWS)
    except ExecutionError:
        blocked = True
    checks["wallet_mismatch_blocks_sign"] = blocked and "create_market_order" not in bad_live.calls

    # rejected order receipt
    rej_client = _FakeClient(wallet_type="DEPOSIT_WALLET", reject=True)
    rej_rec = execute_order(rej_client, exit_intent, plan_live, expected_wallet_type="DEPOSIT_WALLET", ledger_path=tmp_ledger, realized_flows=_SEALED_FLOWS)
    checks["rejected_receipt_marks_unexecuted"] = rej_rec["executed"] is False and rej_rec["receipt"]["ok"] is False

    # deploy accounting: a live BUY fill in the ledger reduces remaining budget
    buy_intent = build_entry_intent(token_id="111222333", order_kind="limit", price=0.5, size=2.0)
    buy_plan = plan_order(
        buy_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    buy_client = _FakeClient(wallet_type="DEPOSIT_WALLET")
    execute_order(buy_client, buy_intent, buy_plan, expected_wallet_type="DEPOSIT_WALLET", ledger_path=tmp_ledger, realized_flows=_SEALED_FLOWS)
    deployed = deployed_usd_so_far(tmp_ledger)
    checks["ledger_tracks_deployed_buy"] = deployed == 1.0  # making_amount from fake accepted

    dup_plan = plan_order(
        buy_intent, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["duplicate_idempotency_rejected"] = dup_plan["would_place"] is False and any(
        "duplicate idempotency" in r for r in dup_plan["fuses"]["refusals"]
    )
    repeat_token_buy = build_entry_intent(
        token_id="111222333",
        order_kind="limit",
        price=0.5,
        size=2.0,
        idempotency_key="different_signal_same_token",
    )
    repeat_token_plan = plan_order(
        repeat_token_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=tmp_ledger,
    )
    checks["add_to_existing_token_not_refused"] = not any(
        "duplicate live BUY exposure for same token" in r for r in repeat_token_plan["fuses"]["refusals"]
    ) and repeat_token_plan["fuses"]["checks"].get("live_buy_token", {}).get("prior_position") is True

    crash_ledger = os.path.join(OUT_DIR, "selftest_pending_crash_ledger.jsonl")
    if os.path.exists(crash_ledger):
        os.remove(crash_ledger)
    crash_buy = build_entry_intent(
        token_id="pending-token",
        order_kind="limit",
        price=0.5,
        size=2.0,
        idempotency_key="selftest_pending_before_post",
    )
    crash_plan = plan_order(
        crash_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=crash_ledger,
    )
    original_append_jsonl = append_jsonl
    crash_client = _FakeClient(wallet_type="DEPOSIT_WALLET")

    def _crash_before_live_executed_append(path: str, row: dict[str, Any]) -> None:
        if path == crash_ledger and row.get("mode") == LIVE_EXECUTED_MODE:
            raise SystemExit("synthetic restart after post_order before LIVE_EXECUTED append")
        original_append_jsonl(path, row)

    crashed_after_post = False
    globals()["append_jsonl"] = _crash_before_live_executed_append
    try:
        try:
            execute_order(crash_client, crash_buy, crash_plan, expected_wallet_type="DEPOSIT_WALLET", ledger_path=crash_ledger, realized_flows=_SEALED_FLOWS)
        except SystemExit:
            crashed_after_post = True
    finally:
        globals()["append_jsonl"] = original_append_jsonl
    restart_same_plan = plan_order(
        crash_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=crash_ledger,
    )
    restart_same_token_plan = plan_order(
        build_entry_intent(
            token_id="pending-token",
            order_kind="limit",
            price=0.5,
            size=2.0,
            idempotency_key="selftest_different_key_same_pending_token",
        ),
        requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=crash_ledger,
    )
    checks["pending_anchor_written_before_post"] = (
        crashed_after_post
        and crash_client.calls[-2:] == ["create_limit_order", "post_order"]
        and LIVE_PENDING_MODE in open(crash_ledger, encoding="utf-8").read()
    )
    # An attempt that never confirmed (orphan PENDING, no LIVE_EXECUTED) MUST be
    # retryable: dedup gates on confirmed execution, never on a bare submission, so
    # a failed/crashed order is not silently treated as already-placed.
    checks["orphan_pending_does_not_block_idempotency_retry"] = restart_same_plan["would_place"] is True
    checks["orphan_pending_does_not_block_same_token_retry"] = restart_same_token_plan["would_place"] is True
    if os.path.exists(crash_ledger):
        os.remove(crash_ledger)

    manual_exposure_ledger = os.path.join(OUT_DIR, "selftest_exposure_ledger.jsonl")
    if os.path.exists(manual_exposure_ledger):
        os.remove(manual_exposure_ledger)
    pending_open_row = {
        "mode": LIVE_PENDING_MODE,
        "idempotency_key": "open-key",
        "intent": {
            "side": "BUY",
            "market_id": "0xmarket",
            "market_slug": "selftest-open-market",
            "token_id": "old-token",
            "outcome": "YES",
            "size": 2.0,
        },
    }
    open_buy_row = {
        "mode": LIVE_EXECUTED_MODE,
        "idempotency_key": "open-key",
        "executed": True,
        "intent": {
            "side": "BUY",
            "market_id": "0xmarket",
            "market_slug": "selftest-open-market",
            "token_id": "old-token",
            "outcome": "YES",
            "size": 2.0,
        },
        "fill": {"filled_shares": 2.0, "filled_notional_usd": 1.0, "order_id": "ord-open"},
        "receipt": {"order_id": "ord-open", "trade_ids": []},
    }
    append_jsonl(manual_exposure_ledger, pending_open_row)
    append_jsonl(manual_exposure_ledger, open_buy_row)
    same_market_buy = build_entry_intent(
        token_id="new-token",
        order_kind="limit",
        price=0.5,
        size=2.0,
        market_id="0xmarket",
        market_slug="selftest-open-market",
        outcome="YES",
        idempotency_key="fresh_key_same_market",
    )
    same_market_plan = plan_order(
        same_market_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=manual_exposure_ledger,
    )
    checks["add_to_open_market_not_refused"] = not any(
        "duplicate open BUY exposure" in r for r in same_market_plan["fuses"]["refusals"]
    ) and same_market_plan["fuses"]["checks"].get("open_market_exposure", {}).get("prior_exposure") is True
    close_row = {
        "mode": LIVE_EXECUTED_MODE,
        "executed": True,
        "intent": {
            "side": "SELL",
            "market_id": "0xmarket",
            "market_slug": "selftest-open-market",
            "token_id": "old-token",
            "outcome": "YES",
            "size": 2.0,
        },
        "fill": {"filled_shares": 2.0, "filled_notional_usd": 1.0, "order_id": "ord-close"},
        "receipt": {"order_id": "ord-close", "trade_ids": []},
    }
    append_jsonl(manual_exposure_ledger, close_row)
    reopened_plan = plan_order(
        same_market_buy, requested_live=True, caps=caps, kill_file=tmp_kill,
        arm_state_file=tmp_ack_valid, ledger_path=manual_exposure_ledger,
    )
    checks["closed_market_exposure_can_reopen"] = reopened_plan["would_place"] is True
    if os.path.exists(manual_exposure_ledger):
        os.remove(manual_exposure_ledger)

    class _UnknownTrade:
        id = "unknown-trade"
        taker_order_id = "ord-unknown"
        side = "BUY"
        price = Decimal("0.5")
        size = Decimal("1.0")
        matched_at = "2026-01-02T00:01:00Z"
        transaction_hash = "0x" + "f" * 64
        maker_orders = ()

    tmp_halt = os.path.join(OUT_DIR, "selftest_HALT")
    if os.path.exists(tmp_halt):
        os.remove(tmp_halt)
    unknown_blocked = False
    try:
        pre_live_account_budget_check(
            _FakeClient(trades=[_UnknownTrade()]),
            buy_intent,
            buy_plan,
            ledger_path=tmp_ledger,
            halt_file=tmp_halt,
        )
    except ExecutionError:
        unknown_blocked = True
    checks["unknown_fill_blocks_only_this_buy_without_halt"] = unknown_blocked and not os.path.exists(tmp_halt)
    if os.path.exists(tmp_halt):
        os.remove(tmp_halt)
    known_by_order_ledger = os.path.join(OUT_DIR, "selftest_known_by_order_ledger.jsonl")
    if os.path.exists(known_by_order_ledger):
        os.remove(known_by_order_ledger)
    append_jsonl(
        known_by_order_ledger,
        {
            "mode": "LIVE_EXECUTED",
            "executed": True,
            "intent": {"side": "BUY", "token_id": "111222333"},
            "receipt": {"ok": True, "order_id": "ord-no-tradeids", "trade_ids": [], "transactions_hashes": []},
            "fill": {"order_id": "ord-no-tradeids", "trade_ids": []},
        },
    )
    class _KnownByOrderTrade:
        id = "trade-later"
        taker_order_id = "ord-no-tradeids"
        side = "BUY"
        price = Decimal("0.5")
        size = Decimal("1.0")
        matched_at = "2026-01-02T00:01:00Z"
        transaction_hash = None
        maker_orders = ()

    known_by_order = pre_live_account_budget_check(
        _FakeClient(trades=[_KnownByOrderTrade()]),
        buy_intent,
        buy_plan,
        ledger_path=known_by_order_ledger,
        halt_file=tmp_halt,
        realized_flows={"buy_usd": 0.5, "sell_usd": 0.0, "redeem_usd": 0.0},
    )
    checks["own_order_id_trade_not_unknown"] = known_by_order["unknown_fills"] == [] and not os.path.exists(tmp_halt)
    if os.path.exists(known_by_order_ledger):
        os.remove(known_by_order_ledger)
    unknown_sell_allowed = False
    try:
        sell_budget = pre_live_account_budget_check(
            _FakeClient(trades=[_UnknownTrade()]),
            exit_intent,
            plan_live,
            ledger_path=tmp_ledger,
            halt_file=tmp_halt,
        )
        unknown_sell_allowed = bool(sell_budget.get("unknown_fills")) and not os.path.exists(tmp_halt)
    except ExecutionError:
        unknown_sell_allowed = False
    checks["unknown_fill_does_not_halt_sell_exit"] = unknown_sell_allowed

    # net-exposure semantics: settled fills no longer consume budget (wins
    # compound without a manual epoch reopen); the drawdown fuse bounds loss.
    compound_ledger = os.path.join(OUT_DIR, "selftest_compound_ledger.jsonl")
    if os.path.exists(compound_ledger):
        os.remove(compound_ledger)
    append_jsonl(
        compound_ledger,
        {
            "mode": "LIVE_EXECUTED",
            "executed": True,
            "intent": {"side": "BUY", "token_id": "999"},
            "receipt": {"ok": True, "order_id": "ord-cycled", "trade_ids": ["tr-cycled"], "transactions_hashes": []},
            "fill": {"order_id": "ord-cycled", "trade_ids": ["tr-cycled"]},
        },
    )

    # Cumulative buys past the total cap, every one of them settled and redeemed
    # at a profit: settled exposure must not keep consuming the budget.
    _cycled_buy_usd = caps.max_total_deploy_usd * 1.2

    class _CycledTrade:
        id = "tr-cycled"
        taker_order_id = "ord-cycled"
        side = "BUY"
        price = Decimal("0.8")
        size = Decimal(str(round(_cycled_buy_usd / 0.8, 6)))
        matched_at = "2026-01-02T00:01:00Z"
        transaction_hash = None
        maker_orders = ()

    if os.path.exists(tmp_halt):
        os.remove(tmp_halt)
    compound_ok = False
    try:
        compound_budget = pre_live_account_budget_check(
            _FakeClient(trades=[_CycledTrade()]),  # positions=[] → at_risk 0
            buy_intent,
            buy_plan,
            ledger_path=compound_ledger,
            halt_file=tmp_halt,
            realized_flows={"buy_usd": _cycled_buy_usd, "sell_usd": 0.0,
                            "redeem_usd": _cycled_buy_usd * 1.25},
        )
        compound_ok = (
            compound_budget["deployed_buy_fills_usd"] > caps.max_total_deploy_usd
            and compound_budget["realized_loss_usd"] < 0
            and not os.path.exists(tmp_halt)
        )
    except ExecutionError:
        compound_ok = False
    checks["net_exposure_lets_settled_wins_compound"] = compound_ok
    if os.path.exists(compound_ledger):
        os.remove(compound_ledger)

    if os.path.exists(tmp_halt):
        os.remove(tmp_halt)
    drawdown_tripped = False
    try:
        pre_live_account_budget_check(
            _FakeClient(trades=[]),
            buy_intent,
            buy_plan,
            ledger_path=tmp_ledger,
            halt_file=tmp_halt,
            # realised loss deliberately past the drawdown cap under test
            realized_flows={"buy_usd": caps.max_drawdown_usd * 1.5,
                            "sell_usd": caps.max_drawdown_usd * 0.2, "redeem_usd": 0.0},
        )
    except ExecutionError:
        drawdown_tripped = True
    checks["drawdown_fuse_trips_halt_on_realized_loss"] = drawdown_tripped and os.path.exists(tmp_halt)
    if os.path.exists(tmp_halt):
        os.remove(tmp_halt)
    safe_budget = pre_live_account_budget_check(
        _FakeClient(trades=[]),
        buy_intent,
        buy_plan,
        ledger_path=tmp_ledger,
        halt_file=tmp_halt,
        realized_flows={"buy_usd": 10.0, "sell_usd": 0.0, "redeem_usd": 15.3},  # won +5.3
    )
    checks["drawdown_fuse_clear_when_winning"] = (
        safe_budget["realized_loss_usd"] < 0 and not os.path.exists(tmp_halt)
    )

    # secret leak guard
    fake_secrets = {"private_key": "SUPERSECRETKEY12345"}
    checks["leak_guard_clean"] = assert_no_secret_leak({"x": "nothing"}, fake_secrets) == []
    checks["leak_guard_catches"] = assert_no_secret_leak({"x": "SUPERSECRETKEY12345"}, fake_secrets) == ["private_key"]

    # cancel path
    cancel_client = _FakeClient(wallet_type="DEPOSIT_WALLET")
    canceled = cancel_order(cancel_client, order_id="ord-live-1")
    checks["cancel_returns_canceled"] = canceled["canceled"] == ["ord-live-1"] and cancel_client.calls == ["cancel_order"]

    for path in (tmp_ledger, tmp_ack_missing, tmp_ack_valid):
        if os.path.exists(path):
            os.remove(path)

    ok = all(checks.values())
    report = {
        "schema_version": "polymarket-execution-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": ok,
        "checks": checks,
    }
    write_json(DEFAULT_SELFTEST, report)
    DEFAULT_GLOBAL_HALT_FILE = saved_global_halt
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Polymarket auto-execution v0.1 (dry_run by default, fuse-gated).")
    p.add_argument("--config", help="Order-intent JSON (action EXIT/ENTER, token_id, etc.).")
    p.add_argument("--live", action="store_true", help="Request live execution (still requires an armed arm-state whose mode permits the side + caps + no kill switch).")
    p.add_argument("--estimate-fill", action="store_true", help="Fetch the public order book for a read-only fill estimate.")
    p.add_argument("--secret-dir", default=DEFAULT_SECRET_DIR)
    p.add_argument("--arm-state-file", default=DEFAULT_ARM_STATE_FILE)
    p.add_argument("--kill-file", default=DEFAULT_KILL_FILE)
    p.add_argument("--expected-wallet-type", default=EXPECTED_WALLET_TYPE)
    p.add_argument("--max-total-deploy-usd", type=float, default=DEFAULT_MAX_TOTAL_DEPLOY_USD)
    p.add_argument("--max-per-trade-usd", type=float, default=DEFAULT_MAX_PER_TRADE_USD)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--latest-json", default=DEFAULT_LATEST_JSON)
    p.add_argument("--latest-summary", default=DEFAULT_LATEST_SUMMARY)
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--engage-kill", action="store_true", help="Engage the kill switch (creates the kill file) and exit.")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["PASS"] else 1
    if args.engage_kill:
        return engage_kill(args.kill_file)
    if args.max_total_deploy_usd <= 0 or args.max_per_trade_usd <= 0:
        print("error: caps must be positive", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
