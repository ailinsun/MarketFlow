#!/usr/bin/env python3
"""Read-only Polymarket private account snapshot v0.1.

This is a private authenticated READ. It loads local Polymarket secret refs,
constructs the official SDK SecureClient against the funder DEPOSIT_WALLET, and
reads the account's current open positions, open orders, recent trades, and an
optional user-websocket connection smoke. It writes a REDACTED ledger so a human
or the live position monitor can consume current account state without re-typing
positions by hand.

Hard boundaries (fail-loud if violated):
  - never print or ledger a private key, API secret, passphrase, or full
    credential value; the wallet/funder address is masked in the ledger;
  - never call order placement / cancel / funds-movement endpoints; only the
    read methods in ALLOWED_SDK_READ_METHODS are ever invoked;
  - the ledger holds only redacted account state and availability status;
  - secrets are never wired into crypto paper.py, the live kernel, or state.mx.

Any live order action stays on a separate path:
  dry_run_preflight_gate -> live_ack_gate -> live_execution_ledger_gate.
This script only does the private_account_readonly_gate.

Run with the local Python 3.11 env that has the Polymarket SDK:
  python3 \
    marketflow/execution/snapshot.py --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any

try:
    from marketflow.execution.sdk_proxy import (
        polymarket_sdk_proxy_url,
        proxied_async_secure_client_transports,
        proxied_secure_client_transports,
    )
except ImportError:  # pragma: no cover - package import fallback.
    from marketflow.execution.sdk_proxy import (
        polymarket_sdk_proxy_url,
        proxied_async_secure_client_transports,
        proxied_secure_client_transports,
    )

from marketflow.execution import orders as pmx


from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
OUT_DIR = runtime_path("execution", "snapshot")
DEFAULT_LEDGER = os.path.join(OUT_DIR, "ledger.jsonl")
DEFAULT_LATEST_JSON = os.path.join(OUT_DIR, "latest.json")
DEFAULT_LATEST_SUMMARY = os.path.join(OUT_DIR, "latest_summary.md")
DEFAULT_SELFTEST = os.path.join(OUT_DIR, "selftest_report.json")
DEFAULT_MONITOR_CONFIG = os.path.join(OUT_DIR, "monitor_positions.json")
DEFAULT_SECRET_DIR = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets")

SCHEMA_VERSION = "polymarket-private-position-snapshot-v0.1"

SECRET_REFS = pmx.SECRET_REFS

READ_ONLY_BOUNDARIES = [
    "private_authenticated_read_only",
    "no_order_placement",
    "no_order_cancel",
    "no_funds_movement",
    "no_secret_print",
    "no_full_credential_in_ledger",
    "wallet_address_masked_in_ledger",
    "scoped_polymarket_sdk_http_proxy",
    "no_seed_phrase",
    "no_crypto_paper_py",
    "no_live_kernel",
    "no_state_mx",
]

# Only these SecureClient methods are ever invoked. Everything that places,
# cancels, signs, or moves funds is forbidden and must never be referenced on a
# call path. The selftest asserts the forbidden methods are not in the allowlist.
ALLOWED_SDK_READ_METHODS = (
    "list_positions",
    "list_open_orders",
    "list_account_trades",
    "subscribe",  # AsyncSecureClient user-websocket read smoke only
    "close",
    "wallet",
    "wallet_type",
)

FORBIDDEN_SDK_METHODS = (
    "post_order",
    "post_orders",
    "place_limit_order",
    "place_market_order",
    "create_limit_order",
    "create_market_order",
    "cancel_order",
    "cancel_orders",
    "cancel_all",
    "cancel_market_orders",
    "redeem_positions",
    "merge_positions",
    "split_position",
    "transfer_erc20",
    "approve_erc20",
    "approve_erc1155_for_all",
    "setup_trading_approvals",
    "setup_gasless_wallet",
)


class SnapshotError(Exception):
    """Raised for fail-loud snapshot errors."""


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
    with open(path, "a", encoding="utf-8") as f:
        f.write(canonical_json(row) + "\n")


def safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def compact_text(value: Any, limit: int = 240) -> str | None:
    text = safe_str(value)
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
        raise SnapshotError(f"secret ref {filename} is empty")
    return value


def load_polymarket_secret_refs(secret_dir: str) -> dict[str, str]:
    try:
        return pmx.load_polymarket_secret_refs(secret_dir)
    except pmx.ExecutionError as exc:
        raise SnapshotError(str(exc)) from exc


def secret_ref_summary(secret_dir: str) -> dict[str, Any]:
    return pmx.secret_ref_summary(secret_dir)


def position_summary(position: Any) -> dict[str, Any]:
    """Redacted summary of an SDK Position; no wallet, no secret."""
    return {
        "condition_id": safe_str(attr(position, "condition_id")),
        "token_id": safe_str(attr(position, "token_id")),
        "size": rounded_float(attr(position, "size")),
        "avg_price": rounded_float(attr(position, "avg_price")),
        "initial_value": rounded_float(attr(position, "initial_value")),
        "current_value": rounded_float(attr(position, "current_value")),
        "cash_pnl": rounded_float(attr(position, "cash_pnl")),
        "cur_price": rounded_float(attr(position, "cur_price")),
        "title": compact_text(attr(position, "title")),
        "slug": safe_str(attr(position, "slug")),
        "event_slug": safe_str(attr(position, "event_slug")),
        "outcome": safe_str(attr(position, "outcome")),
        "outcome_index": attr(position, "outcome_index"),
        "opposite_token_id": safe_str(attr(position, "opposite_token_id")),
        "redeemable": attr(position, "redeemable"),
        "negative_risk": attr(position, "negative_risk"),
    }


def open_order_summary(order: Any) -> dict[str, Any]:
    """Redacted summary of an SDK OpenOrder; owner/maker addresses masked."""
    return {
        "order_id": safe_str(attr(order, "id")),
        "market": safe_str(attr(order, "market")),
        "token_id": safe_str(attr(order, "token_id")),
        "owner_masked": mask_address(attr(order, "owner")),
        "maker_address_masked": mask_address(attr(order, "maker_address")),
        "side": safe_str(attr(order, "side")),
        "price": rounded_float(attr(order, "price")),
        "original_size": rounded_float(attr(order, "original_size")),
        "size_matched": rounded_float(attr(order, "size_matched")),
        "outcome": safe_str(attr(order, "outcome")),
        "order_type": safe_str(attr(order, "order_type")),
        "status": safe_str(attr(order, "status")),
        "created_at": safe_str(attr(order, "created_at")),
        "expires_at": safe_str(attr(order, "expires_at")),
    }


def clob_trade_summary(trade: Any) -> dict[str, Any]:
    """Redacted summary of an SDK ClobTrade; owner/maker addresses masked."""
    return {
        "trade_id": safe_str(attr(trade, "id")),
        "market": safe_str(attr(trade, "market")),
        "token_id": safe_str(attr(trade, "token_id")),
        "owner_masked": mask_address(attr(trade, "owner")),
        "maker_address_masked": mask_address(attr(trade, "maker_address")),
        "side": safe_str(attr(trade, "side")),
        "trader_side": safe_str(attr(trade, "trader_side")),
        "price": rounded_float(attr(trade, "price")),
        "size": rounded_float(attr(trade, "size")),
        "outcome": safe_str(attr(trade, "outcome")),
        "status": safe_str(attr(trade, "status")),
        "fee_rate_bps": safe_str(attr(trade, "fee_rate_bps")),
        "transaction_hash": safe_str(attr(trade, "transaction_hash")),
        "matched_at": safe_str(attr(trade, "matched_at")),
    }


def collect_paginator(paginator: Any, *, max_items: int) -> tuple[list[Any], dict[str, Any]]:
    """Drain a SDK Paginator up to max_items; return (items, page_meta)."""
    items: list[Any] = []
    pages_seen = 0
    has_more = False
    total_count: int | None = None
    truncated = False
    for page in paginator:
        pages_seen += 1
        total_count = attr(page, "total_count")
        has_more = bool(attr(page, "has_more"))
        for item in attr(page, "items") or []:
            items.append(item)
            if len(items) >= max_items:
                truncated = True
                has_more = True
                break
        if truncated or not has_more:
            break
    return items, {
        "pages_seen": pages_seen,
        "has_more": has_more,
        "total_count": total_count,
        "truncated_by_max_items": truncated,
        "returned": len(items),
    }


def build_secure_client(secrets: dict[str, str], *, secret_dir: str = DEFAULT_SECRET_DIR) -> Any:
    try:
        return pmx.build_secure_client(secrets, secret_dir=secret_dir, side="BUY")
    except pmx.ExecutionError as exc:
        raise SnapshotError(str(exc)) from exc


async def _user_ws_smoke(
    secrets: dict[str, str],
    state: dict[str, Any],
    *,
    secret_dir: str,
    max_events: int,
) -> None:
    from polymarket import ApiKeyCreds, AsyncSecureClient
    from polymarket.streams import UserSpec

    credentials = ApiKeyCreds(
        key=secrets["api_key"],
        secret=secrets["api_secret"],
        passphrase=secrets["passphrase"],
    )
    client = None
    handle = None
    try:
        proxy_url = polymarket_sdk_proxy_url(secret_dir)
        with proxied_async_secure_client_transports(proxy_url):
            client = await AsyncSecureClient.create(
                private_key=secrets["private_key"],
                wallet=secrets["funder_address"],
                credentials=credentials,
            )
        handle = await client.subscribe(UserSpec())
        state["connected"] = True
        async for event in handle:
            key = type(event).__name__
            state["event_type_counts"][key] = state["event_type_counts"].get(key, 0) + 1
            state["events_seen"] += 1
            if state["events_seen"] >= max_events:
                break
    finally:
        if handle is not None:
            try:
                await handle.close()
            except Exception:  # pragma: no cover - best-effort close.
                pass
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - best-effort close.
                pass


def user_ws_smoke(
    secrets: dict[str, str],
    *,
    secret_dir: str,
    timeout_s: float,
    max_events: int,
) -> dict[str, Any]:
    if "private_key" not in secrets:
        return {
            "requested": True,
            "connected": False,
            "events_seen": 0,
            "event_type_counts": {},
            "channel": "wss user (read-only)",
            "status": "skipped_turnkey",
            "note": "the async SDK requires a raw key; Turnkey migration keeps it unavailable",
        }
    state: dict[str, Any] = {
        "requested": True,
        "connected": False,
        "events_seen": 0,
        "event_type_counts": {},
        "channel": "wss user (read-only)",
        "note": "connection within the window is the smoke; zero events is normal for a quiet account",
    }

    async def runner() -> dict[str, Any]:
        try:
            await asyncio.wait_for(
                _user_ws_smoke(secrets, state, secret_dir=secret_dir, max_events=max_events),
                timeout=timeout_s,
            )
            state["status"] = "connected" if state["connected"] else "no_connection"
        except asyncio.TimeoutError:
            state["status"] = "connected" if state["connected"] else "connect_timeout"
        return state

    try:
        return asyncio.run(runner())
    except Exception as exc:
        state["status"] = "error"
        state["error"] = compact_text(exc)
        return state


def positions_to_monitor_config(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape redacted positions into the live position monitor's config schema."""
    monitor_positions = []
    for p in positions:
        size = to_float(p.get("size"))
        token_id = p.get("token_id")
        condition_id = p.get("condition_id")
        if size is None or size <= 0 or not token_id or not condition_id:
            continue
        entry_cost = p.get("initial_value")
        if entry_cost is None and p.get("avg_price") is not None:
            entry_cost = round(to_float(p["avg_price"]) * size, 8)
        monitor_positions.append(
            {
                "market_id": condition_id,
                "market_slug": p.get("slug"),
                "token_id": token_id,
                "side": p.get("outcome") or "HELD",
                "shares": round(size, 8),
                "entry_cost": entry_cost,
                "_source": "polymarket_private_position_snapshot",
            }
        )
    return {
        "schema_version": "polymarket-live-position-monitor-config-v0.1",
        "generated_at": iso_now(),
        "source": "polymarket_private_position_snapshot",
        "note": "feed individual entries to polymarket_live_position_monitor.py --config; not an order trigger",
        "positions": monitor_positions,
    }


def take_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    secrets = load_polymarket_secret_refs(args.secret_dir)
    client = build_secure_client(secrets, secret_dir=args.secret_dir)
    availability = {"auth_ok": True}
    account: dict[str, Any] = {
        "source": "polymarket_secure_client",
        "operation": "SecureClient.create + read-only list_* paginators",
        "read_only": True,
        "secret_refs": secret_ref_summary(args.secret_dir),
        "funder_address_masked": mask_address(secrets["funder_address"]),
    }
    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    recent_trades: list[dict[str, Any]] = []
    positions_meta: dict[str, Any] = {}
    orders_meta: dict[str, Any] = {}
    trades_meta: dict[str, Any] = {}
    try:
        account["wallet_type"] = safe_str(attr(client, "wallet_type"))
        account["signer_masked"] = mask_address(attr(client, "wallet"))

        try:
            raw, positions_meta = collect_paginator(
                client.list_positions(page_size=args.page_size), max_items=args.max_items
            )
            positions = [position_summary(p) for p in raw]
            availability["positions_ok"] = True
        except Exception as exc:
            positions_meta = {"error": compact_text(exc)}
            availability["positions_ok"] = False

        try:
            raw, orders_meta = collect_paginator(
                client.list_open_orders(), max_items=args.max_items
            )
            open_orders = [open_order_summary(o) for o in raw]
            availability["open_orders_ok"] = True
        except Exception as exc:
            orders_meta = {"error": compact_text(exc)}
            availability["open_orders_ok"] = False

        try:
            raw, trades_meta = collect_paginator(
                client.list_account_trades(), max_items=args.max_items
            )
            recent_trades = [clob_trade_summary(t) for t in raw]
            availability["recent_trades_ok"] = True
        except Exception as exc:
            trades_meta = {"error": compact_text(exc)}
            availability["recent_trades_ok"] = False
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - best-effort close.
            pass

    ws = None
    if args.user_ws_smoke:
        ws = user_ws_smoke(
            secrets,
            secret_dir=args.secret_dir,
            timeout_s=args.ws_timeout,
            max_events=args.ws_max_events,
        )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "snapshot_id": f"pmpps_{now_ms()}_{hash_obj(account)[:10]}",
        "mode": "PRIVATE_READ_ONLY_SNAPSHOT",
        "gate": "private_account_readonly_gate",
        "boundaries": READ_ONLY_BOUNDARIES,
        "account": account,
        "positions": {"meta": positions_meta, "items": positions},
        "open_orders": {"meta": orders_meta, "items": open_orders},
        "recent_trades": {"meta": trades_meta, "items": recent_trades},
        "user_ws_smoke": ws,
        "availability": availability,
    }
    return snapshot


def assert_no_secret_leak(snapshot: dict[str, Any], secrets: dict[str, str]) -> list[str]:
    """Fail-loud guard: no raw secret value may appear anywhere in the ledger row."""
    blob = canonical_json(snapshot)
    leaks = []
    for key, value in secrets.items():
        if value and len(value) >= 8 and value in blob:
            leaks.append(key)
    return leaks


def human_summary(snapshot: dict[str, Any]) -> str:
    acct = snapshot.get("account", {})
    pos = snapshot.get("positions", {})
    orders = snapshot.get("open_orders", {})
    trades = snapshot.get("recent_trades", {})
    avail = snapshot.get("availability", {})
    ws = snapshot.get("user_ws_smoke")
    lines = [
        "# Polymarket Private Account Snapshot v0.1",
        "",
        f"- generated_at: `{snapshot.get('generated_at')}`",
        f"- mode: `{snapshot.get('mode')}`",
        f"- gate: `{snapshot.get('gate')}`",
        f"- wallet_type: `{acct.get('wallet_type')}`",
        f"- funder_address_masked: `{acct.get('funder_address_masked')}`",
        f"- auth_ok: `{avail.get('auth_ok')}`",
        "",
        "## Counts",
        "",
        f"- open positions: `{len(pos.get('items', []))}` (has_more=`{pos.get('meta', {}).get('has_more')}`)",
        f"- open orders: `{len(orders.get('items', []))}` (has_more=`{orders.get('meta', {}).get('has_more')}`)",
        f"- recent trades: `{len(trades.get('items', []))}` (has_more=`{trades.get('meta', {}).get('has_more')}`)",
    ]
    if ws is not None:
        lines.append(f"- user_ws_smoke: `{ws.get('status')}` (events_seen=`{ws.get('events_seen')}`)")
    lines.extend(["", "## Open Positions", ""])
    if not pos.get("items"):
        lines.append("- none")
    for idx, p in enumerate(pos.get("items", []), start=1):
        lines.append(
            f"{idx}. {p.get('title') or p.get('slug') or 'position'} | "
            f"outcome=`{p.get('outcome')}` size=`{p.get('size')}` "
            f"cur_price=`{p.get('cur_price')}` cash_pnl=`{p.get('cash_pnl')}`"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "Private authenticated READ only. No order placement/cancel, no funds movement, "
            "no secret printing, no full credential in ledger, "
            "no crypto paper.py / live kernel / state.mx.",
            "",
        ]
    )
    return "\n".join(lines)


def run_snapshot(args: argparse.Namespace) -> int:
    secrets_for_guard: dict[str, str] = {}
    try:
        secrets_for_guard = load_polymarket_secret_refs(args.secret_dir)
        snapshot = take_snapshot(args)
        leaks = assert_no_secret_leak(snapshot, secrets_for_guard)
        if leaks:
            raise SnapshotError(f"secret leak guard tripped for: {', '.join(sorted(leaks))}")
    except Exception as exc:
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_now(),
            "mode": "PRIVATE_READ_ONLY_SNAPSHOT",
            "gate": "private_account_readonly_gate",
            "boundaries": READ_ONLY_BOUNDARIES,
            "account": {"source": "polymarket_secure_client", "read_only": True},
            "availability": {"auth_ok": False},
            "error": compact_text(exc),
        }

    append_jsonl(args.ledger, snapshot)
    write_json(args.latest_json, snapshot)
    ensure_parent(args.latest_summary)
    with open(args.latest_summary, "w", encoding="utf-8") as f:
        f.write(human_summary(snapshot))

    if not args.no_monitor_config and snapshot.get("availability", {}).get("positions_ok"):
        write_json(
            args.monitor_config,
            positions_to_monitor_config(snapshot.get("positions", {}).get("items", [])),
        )

    if args.print_json:
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    else:
        avail = snapshot.get("availability", {})
        print(
            f"{snapshot['generated_at']} auth_ok={avail.get('auth_ok')} "
            f"positions={len(snapshot.get('positions', {}).get('items', []))} "
            f"open_orders={len(snapshot.get('open_orders', {}).get('items', []))} "
            f"recent_trades={len(snapshot.get('recent_trades', {}).get('items', []))}"
        )
    return 0 if snapshot.get("availability", {}).get("auth_ok") else 2


def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    checks["forbidden_methods_not_in_allowlist"] = not (
        set(FORBIDDEN_SDK_METHODS) & set(ALLOWED_SDK_READ_METHODS)
    )

    checks["mask_address_keeps_head_tail"] = mask_address(
        "0x" + "abcd" + "0" * 32 + "ef01"
    ) == "0xabcd…ef01"
    checks["mask_address_short"] = mask_address("0x1234") == "0x" + "*" * 4
    checks["mask_address_none"] = mask_address(None) is None

    class FakePosition:
        condition_id = "0x" + "1" * 64
        token_id = "111222333"
        size = Decimal("62.5")
        avg_price = Decimal("0.08")
        initial_value = Decimal("5.0")
        current_value = Decimal("4.06")
        cash_pnl = Decimal("-0.94")
        cur_price = Decimal("0.065")
        title = "Will example resolve YES?"
        slug = "example-market"
        event_slug = "example-event"
        outcome = "YES"
        outcome_index = 0
        opposite_token_id = "444555666"
        redeemable = False
        negative_risk = False

    psum = position_summary(FakePosition())
    checks["position_summary_size"] = psum["size"] == 62.5
    checks["position_summary_outcome"] = psum["outcome"] == "YES"
    checks["position_summary_no_wallet_key"] = not any(
        "owner" in k or "wallet" in k for k in psum
    )

    class FakeOrder:
        id = "ord-1"
        market = "0x" + "2" * 64
        token_id = "111222333"
        owner = "0x" + "a" * 40
        maker_address = "0x" + "b" * 40
        side = "BUY"
        price = Decimal("0.10")
        original_size = Decimal("100")
        size_matched = Decimal("0")
        outcome = "YES"
        order_type = "GTC"
        status = "LIVE"
        created_at = "2026-06-19T00:00:00Z"
        expires_at = None

    osum = open_order_summary(FakeOrder())
    checks["order_summary_owner_masked"] = osum["owner_masked"].startswith("0xaaaa")
    checks["order_summary_no_raw_owner"] = ("a" * 40) not in canonical_json(osum)
    checks["order_summary_side"] = osum["side"] == "BUY"

    class FakeTrade:
        id = "trd-1"
        market = "0x" + "3" * 64
        token_id = "111222333"
        owner = "0x" + "c" * 40
        maker_address = "0x" + "d" * 40
        side = "SELL"
        trader_side = "TAKER"
        price = Decimal("0.09")
        size = Decimal("10")
        outcome = "YES"
        status = "CONFIRMED"
        fee_rate_bps = "0"
        transaction_hash = "0x" + "e" * 64
        matched_at = "2026-06-18T12:00:00Z"

    tsum = clob_trade_summary(FakeTrade())
    checks["trade_summary_maker_masked"] = tsum["maker_address_masked"].startswith("0xdddd")
    checks["trade_summary_keeps_txhash"] = tsum["transaction_hash"] == "0x" + "e" * 64

    mon = positions_to_monitor_config([psum])
    checks["monitor_config_one_position"] = len(mon["positions"]) == 1
    checks["monitor_config_token_id"] = mon["positions"][0]["token_id"] == "111222333"
    checks["monitor_config_shares"] = mon["positions"][0]["shares"] == 62.5

    fake_secrets = {"private_key": "SUPERSECRETKEY12345", "api_secret": "anothersecretvalue999"}
    leak_blob = {"x": "no secret here", "y": position_summary(FakePosition())}
    checks["no_secret_leak_clean"] = assert_no_secret_leak(leak_blob, fake_secrets) == []
    leaky = {"oops": "SUPERSECRETKEY12345"}
    checks["secret_leak_detected"] = assert_no_secret_leak(leaky, fake_secrets) == ["private_key"]

    coll_items, coll_meta = collect_paginator(
        iter(
            [
                type("Pg", (), {"items": [1, 2], "has_more": True, "total_count": 5})(),
                type("Pg", (), {"items": [3], "has_more": False, "total_count": 5})(),
            ]
        ),
        max_items=10,
    )
    checks["paginator_drain"] = coll_items == [1, 2, 3] and coll_meta["has_more"] is False
    trunc_items, trunc_meta = collect_paginator(
        iter([type("Pg", (), {"items": [1, 2, 3], "has_more": True, "total_count": 9})()]),
        max_items=2,
    )
    checks["paginator_truncates"] = (
        trunc_items == [1, 2] and trunc_meta["truncated_by_max_items"] is True
    )

    ok = all(checks.values())
    report = {
        "schema_version": "polymarket-private-position-snapshot-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": ok,
        "checks": checks,
    }
    write_json(DEFAULT_SELFTEST, report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only Polymarket private account snapshot v0.1.")
    p.add_argument("--secret-dir", default=DEFAULT_SECRET_DIR, help="Directory with Polymarket secret refs.")
    p.add_argument("--page-size", type=int, default=20, help="SDK paginator page size.")
    p.add_argument("--max-items", type=int, default=50, help="Max items per read category per snapshot.")
    p.add_argument("--user-ws-smoke", action="store_true", help="Optional read-only user websocket connection smoke.")
    p.add_argument("--ws-timeout", type=float, default=8.0, help="User websocket smoke window in seconds.")
    p.add_argument("--ws-max-events", type=int, default=5, help="Max user websocket events to sample.")
    p.add_argument("--no-monitor-config", action="store_true", help="Do not emit a monitor position config.")
    p.add_argument("--once", action="store_true", help="Run a single snapshot (default).")
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--latest-json", default=DEFAULT_LATEST_JSON)
    p.add_argument("--latest-summary", default=DEFAULT_LATEST_SUMMARY)
    p.add_argument("--monitor-config", default=DEFAULT_MONITOR_CONFIG)
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["PASS"] else 1
    if args.page_size <= 0:
        print("error: --page-size must be positive", file=sys.stderr)
        return 2
    if args.max_items <= 0:
        print("error: --max-items must be positive", file=sys.stderr)
        return 2
    return run_snapshot(args)


if __name__ == "__main__":
    raise SystemExit(main())
