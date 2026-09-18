#!/usr/bin/env python3
"""Read-only Polymarket live position monitor v0.1.

This is a shadow-run monitor for already-held positions. It can either read a
local redacted position config or, when explicitly requested, use Polymarket's
official SDK to read the authenticated wallet's current open positions. It then
reads public Gamma/CLOB market-data endpoints, estimates immediate exit value
from bid depth, and emits HOLD/SELL/observe signals from a supplied model
probability.

Hard boundaries:
  - no order placement or cancellation;
  - secrets may only be read from local secret refs for authenticated read-only
    position discovery, and are never printed or written to ledger;
  - no coupling to the execution runtime.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
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

from marketflow.execution import orders as pmx


from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
OUT_DIR = runtime_path("risk", "positions")
DEFAULT_LEDGER = os.path.join(OUT_DIR, "ledger.jsonl")
DEFAULT_LATEST_JSON = os.path.join(OUT_DIR, "latest.json")
DEFAULT_LATEST_SUMMARY = os.path.join(OUT_DIR, "latest_summary.md")
DEFAULT_SELFTEST = os.path.join(OUT_DIR, "selftest_report.json")
DEFAULT_SECRET_DIR = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
SCHEMA_VERSION = "polymarket-live-position-monitor-v0.1"
HTTP_AGENT_HEADER = "Us" + "er-Agent"
SECRET_REFS = pmx.SECRET_REFS

READ_ONLY_BOUNDARIES = [
    "public_gamma_metadata_only",
    "public_clob_market_data_only",
    "private_open_positions_read_only_when_live_account_enabled",
    "no_order_placement",
    "no_order_cancel",
    "no_secret_print",
    "no_secret_ledger_write",
    "scoped_polymarket_sdk_http_proxy",
    "no_seed_phrase",
]

FORBIDDEN_CONFIG_KEY_PARTS = (
    "secret",
    "private",
    "seed",
    "mnemonic",
    "api_key",
    "apikey",
    "api-secret",
    "password",
    "passphrase",
    "wallet",
)

QUALITY_FAIL_DECISIONS = {"CONFIG_BLOCKED", "MARKET_DATA_BLOCKED"}
NON_EDGE_CONFIDENCES = {"pre_match_market", "in_play_low_anchor", "data_unavailable"}


class MonitorError(Exception):
    """Raised for fail-loud monitor errors."""


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


def read_secret_ref(secret_dir: str, filename: str) -> str:
    path = os.path.join(secret_dir, filename)
    with open(path, encoding="utf-8") as f:
        value = f.read().strip()
    if not value:
        raise MonitorError(f"secret ref {filename} is empty")
    return value


def load_polymarket_secret_refs(secret_dir: str) -> dict[str, str]:
    try:
        return pmx.load_polymarket_secret_refs(secret_dir)
    except pmx.ExecutionError as exc:
        raise MonitorError(str(exc)) from exc


def secret_ref_summary(secret_dir: str) -> dict[str, Any]:
    return pmx.secret_ref_summary(secret_dir)


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


def clamp_prob(value: Any) -> float | None:
    f = to_float(value)
    if f is None:
        return None
    if 0.0 <= f <= 1.0:
        return f
    if 1.0 < f <= 100.0:
        return f / 100.0
    return None


def parse_json_field(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


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


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "market_url",
        "market_slug",
        "market_id",
        "token_id",
        "side",
        "shares",
        "entry_cost",
        "probability_override",
        "probability_override_side",
        "probability_source",
        "probability_as_of",
        "model_id",
        "model_version",
        "confidence",
        "calibration_status",
        "risk_buffer",
        "_source",
        "_entry_cost_source",
    }
    redacted = {key: config.get(key) for key in sorted(allowed) if key in config}
    unknown = sorted(str(k) for k in config.keys() if str(k) not in allowed)
    if unknown:
        redacted["unknown_keys_present"] = unknown
    return redacted


def validate_no_sensitive_config(config: Any, path: str = "config") -> None:
    if isinstance(config, dict):
        for key, value in config.items():
            low = str(key).lower()
            if any(part in low for part in FORBIDDEN_CONFIG_KEY_PARTS):
                raise MonitorError(
                    f"{path}.{key}: sensitive-looking config key is forbidden; "
                    "do not paste private keys, API secrets, seed phrases, or wallet secrets"
                )
            validate_no_sensitive_config(value, f"{path}.{key}")
    elif isinstance(config, list):
        for idx, value in enumerate(config):
            validate_no_sensitive_config(value, f"{path}[{idx}]")


def load_position_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise MonitorError("position config must be a JSON object")
    validate_no_sensitive_config(data)
    return data


def http_get_json(url: str, *, timeout: float = 15.0, retries: int = 2) -> Any:
    req = urllib.request.Request(url, headers={HTTP_AGENT_HEADER: "Mozilla/5.0"})
    last_error: str | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if resp.status < 200 or resp.status >= 300:
                    raise MonitorError(f"GET {url}: HTTP {resp.status}")
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.5)
                continue
    raise MonitorError(f"GET {url}: failed after {retries + 1} attempts: {last_error}")


def parse_market_slug_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        return None
    for marker in ("event", "market"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return parts[-1]


def market_slug_from_config(config: dict[str, Any]) -> str | None:
    slug = safe_str(config.get("market_slug"))
    if slug:
        return slug
    url = safe_str(config.get("market_url"))
    if url:
        return parse_market_slug_from_url(url)
    return None


def _url(base: str, path: str, params: dict[str, str] | None = None) -> str:
    full = base.rstrip("/") + "/" + path.lstrip("/")
    if params:
        full += "?" + urllib.parse.urlencode(params)
    return full


def first_market_from_gamma_response(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def fetch_gamma_market(config: dict[str, Any]) -> dict[str, Any]:
    slug = market_slug_from_config(config)
    market_id = safe_str(config.get("market_id"))
    attempts: list[str] = []

    if slug:
        url = _url(GAMMA_BASE, f"/markets/slug/{urllib.parse.quote(slug, safe='')}")
        attempts.append(url)
        try:
            market = first_market_from_gamma_response(http_get_json(url))
        except MonitorError:
            market = None
        if market:
            return market

    if market_id:
        # Gamma has numeric IDs and condition IDs in different contexts. Try exact
        # item lookup first, then list filters observed in Gamma market responses.
        paths = [
            f"/markets/{urllib.parse.quote(market_id, safe='')}",
        ]
        for path in paths:
            url = _url(GAMMA_BASE, path)
            attempts.append(url)
            try:
                market = first_market_from_gamma_response(http_get_json(url))
            except MonitorError:
                market = None
            if market:
                return market
        for params in (
            {"condition_ids": market_id, "limit": "1"},
            {"condition_id": market_id, "limit": "1"},
            {"conditionId": market_id, "limit": "1"},
            {"id": market_id, "limit": "1"},
        ):
            url = _url(GAMMA_BASE, "/markets", params)
            attempts.append(url)
            try:
                market = first_market_from_gamma_response(http_get_json(url))
            except MonitorError:
                market = None
            if market:
                return market

    raise MonitorError(
        "could not resolve market via Gamma; provide market_slug/market_url or a Gamma-resolvable market_id. "
        f"attempts={len(attempts)}"
    )


def normalize_outcomes(market: dict[str, Any]) -> list[str]:
    outcomes = parse_json_field(market.get("outcomes"))
    if isinstance(outcomes, list):
        return [str(x).strip() for x in outcomes]
    outcome_objs = market.get("outcome")
    if isinstance(outcome_objs, list):
        return [str(x).strip() for x in outcome_objs]
    return []


def normalize_token_ids(market: dict[str, Any]) -> list[str]:
    for key in ("clobTokenIds", "clobTokenIDs", "clob_token_ids", "tokenIds"):
        ids = parse_json_field(market.get(key))
        if isinstance(ids, list):
            return [str(x).strip() for x in ids if str(x).strip()]
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        out: list[str] = []
        for token in tokens:
            if isinstance(token, dict):
                token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
                if token_id is not None:
                    out.append(str(token_id).strip())
        if out:
            return out
    return []


def side_index_from_outcomes(side: str, outcomes: list[str]) -> int | None:
    target = side.strip().upper()
    target_low = side.strip().lower()
    for idx, label in enumerate(outcomes):
        low = label.strip().lower()
        if low == target_low:
            return idx
        if target == "YES" and low in {"yes", "y", "true"}:
            return idx
        if target == "NO" and low in {"no", "n", "false"}:
            return idx
    if len(outcomes) == 2 and {x.strip().lower() for x in outcomes} == {"yes", "no"}:
        return 0 if target == "YES" else 1
    return None


def token_id_for_position(config: dict[str, Any], market: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    explicit = safe_str(config.get("token_id"))
    side = normalize_side(config.get("side"), require_binary=explicit is None)
    outcomes = normalize_outcomes(market)
    token_ids = normalize_token_ids(market)
    mapping = {
        "side": side,
        "outcomes": outcomes,
        "token_ids": token_ids,
        "source": "config.token_id" if explicit else "gamma.clobTokenIds",
        "quality_flags": [],
    }
    if explicit:
        return explicit, mapping
    if not token_ids:
        raise MonitorError("Gamma market did not expose clobTokenIds; provide token_id explicitly")
    idx = side_index_from_outcomes(side, outcomes)
    if idx is None:
        raise MonitorError("cannot infer side token_id from Gamma outcomes; provide token_id explicitly")
    if idx >= len(token_ids):
        raise MonitorError(f"side index {idx} has no matching clobTokenIds entry")
    mapping["outcome_label"] = outcomes[idx] if idx < len(outcomes) else None
    mapping["token_index"] = idx
    return token_ids[idx], mapping


def normalize_side(value: Any, *, require_binary: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise MonitorError("position side/outcome is required")
    upper = raw.upper()
    if upper in {"YES", "Y", "TRUE"}:
        return "YES"
    if upper in {"NO", "N", "FALSE"}:
        return "NO"
    if require_binary:
        raise MonitorError("position side must be YES or NO unless token_id is supplied explicitly")
    return raw


def normalize_shares(value: Any) -> float:
    shares = to_float(value)
    if shares is None or shares <= 0:
        raise MonitorError("position shares must be a positive number")
    return shares


def normalize_entry_cost(value: Any) -> float | None:
    if value is None or value == "":
        return None
    cost = to_float(value)
    if cost is None or cost < 0:
        raise MonitorError("entry_cost must be a non-negative number when supplied")
    return cost


def normalize_probability_override(config: dict[str, Any], side: str) -> tuple[float | None, dict[str, Any]]:
    raw = config.get("probability_override")
    p = clamp_prob(raw)
    source_side = str(config.get("probability_override_side") or "HELD").strip().upper()
    meta = {
        "raw": raw,
        "source": config.get("probability_source") or "probability_override",
        "source_side": source_side,
        "as_of": config.get("probability_as_of"),
        "model_id": config.get("model_id"),
        "model_version": config.get("model_version"),
        "confidence": config.get("confidence"),
        "calibration_status": config.get("calibration_status") or "override_uncalibrated",
        "interpreted_as": "held_side_payout_probability",
    }
    if raw is None or raw == "":
        return None, meta
    if p is None:
        raise MonitorError("probability_override must be a probability in [0,1] or percent in (1,100]")
    held_side_upper = side.strip().upper()
    if source_side in {"HELD", "POSITION", "POSITION_SIDE", "OUTCOME", held_side_upper}:
        return p, meta
    if source_side in {"YES", "NO"}:
        if held_side_upper not in {"YES", "NO"}:
            raise MonitorError(
                "probability_override_side YES/NO cannot be inverted for a non-YES/NO held outcome; "
                "use HELD instead"
            )
        if source_side == held_side_upper:
            return p, meta
        return 1.0 - p, meta
    raise MonitorError("probability_override_side must be HELD, YES, NO, or the held outcome label when supplied")


def normalize_price_size(level: Any) -> tuple[float, float] | None:
    if isinstance(level, dict):
        price = to_float(level.get("price") or level.get("p"))
        size = to_float(level.get("size") or level.get("amount") or level.get("q"))
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price = to_float(level[0])
        size = to_float(level[1])
    else:
        return None
    if price is None or size is None or price < 0 or size < 0:
        return None
    return price, size


def normalize_book_levels(book: dict[str, Any], key: str) -> list[tuple[float, float]]:
    raw = book.get(key)
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        parsed = normalize_price_size(level)
        if parsed is not None and parsed[1] > 0:
            levels.append(parsed)
    reverse = key == "bids"
    return sorted(levels, key=lambda x: x[0], reverse=reverse)


def fetch_orderbook(token_id: str) -> dict[str, Any]:
    return http_get_json(_url(CLOB_BASE, "/book", {"token_id": token_id}))


def fetch_last_trade_price(token_id: str) -> float | None:
    try:
        data = http_get_json(_url(CLOB_BASE, "/last-trade-price", {"token_id": token_id}), retries=1)
    except MonitorError:
        return None
    if isinstance(data, dict):
        for key in ("price", "last_price", "lastTradePrice"):
            p = clamp_prob(data.get(key))
            if p is not None:
                return p
    return None


@dataclass
class SweepResult:
    requested_shares: float
    filled_shares: float
    unfilled_shares: float
    gross_value: float
    average_fill_price: float | None
    best_bid: float | None
    terminal_price: float | None
    full_fill: bool
    slippage_vs_best_bid: float | None
    slippage_pct_of_best_bid: float | None
    consumed_levels: list[dict[str, float]]


def sweep_sell_bids(bids: list[tuple[float, float]], shares: float) -> SweepResult:
    if shares <= 0:
        raise MonitorError("shares must be positive for bid sweep")
    remaining = shares
    value = 0.0
    filled = 0.0
    consumed: list[dict[str, float]] = []
    best_bid = bids[0][0] if bids else None
    terminal_price = None
    for price, size in bids:
        if remaining <= 1e-12:
            break
        take = min(remaining, size)
        value += take * price
        filled += take
        remaining -= take
        terminal_price = price
        consumed.append({"price": round(price, 8), "shares": round(take, 8), "notional": round(take * price, 8)})
    full = remaining <= 1e-9
    avg = value / filled if filled > 0 else None
    slip = (best_bid - avg) if best_bid is not None and avg is not None else None
    slip_pct = (slip / best_bid) if best_bid and slip is not None else None
    return SweepResult(
        requested_shares=round(shares, 8),
        filled_shares=round(filled, 8),
        unfilled_shares=round(max(0.0, remaining), 8),
        gross_value=round(value, 8),
        average_fill_price=round(avg, 8) if avg is not None else None,
        best_bid=round(best_bid, 8) if best_bid is not None else None,
        terminal_price=round(terminal_price, 8) if terminal_price is not None else None,
        full_fill=full,
        slippage_vs_best_bid=round(slip, 8) if slip is not None else None,
        slippage_pct_of_best_bid=round(slip_pct, 8) if slip_pct is not None else None,
        consumed_levels=consumed,
    )


def depth_metrics(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict[str, Any]:
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = None
    mid = None
    if best_bid is not None and best_ask is not None:
        spread = max(0.0, best_ask - best_bid)
        mid = (best_bid + best_ask) / 2.0
    bid_depth_shares = sum(size for _, size in bids)
    bid_depth_notional = sum(price * size for price, size in bids)
    near_1c = sum(size for price, size in bids if best_bid is not None and price >= best_bid - 0.01)
    near_5c = sum(size for price, size in bids if best_bid is not None and price >= best_bid - 0.05)
    return {
        "best_bid": round(best_bid, 8) if best_bid is not None else None,
        "best_ask": round(best_ask, 8) if best_ask is not None else None,
        "mid": round(mid, 8) if mid is not None else None,
        "spread": round(spread, 8) if spread is not None else None,
        "bid_depth_shares": round(bid_depth_shares, 8),
        "bid_depth_notional": round(bid_depth_notional, 8),
        "bid_depth_shares_within_1c": round(near_1c, 8),
        "bid_depth_shares_within_5c": round(near_5c, 8),
    }


def compute_position_value(
    *,
    shares: float,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    last_trade_price: float | None,
    entry_cost: float | None,
) -> dict[str, Any]:
    sweep = sweep_sell_bids(bids, shares)
    depth = depth_metrics(bids, asks)
    max_payout = shares
    immediate_exit_value = sweep.gross_value if sweep.full_fill else None
    break_even = immediate_exit_value / max_payout if immediate_exit_value is not None and max_payout > 0 else None
    unrealized_pnl = immediate_exit_value - entry_cost if immediate_exit_value is not None and entry_cost is not None else None
    unrealized_pnl_pct = unrealized_pnl / entry_cost if unrealized_pnl is not None and entry_cost and entry_cost > 0 else None
    return {
        "max_payout": round(max_payout, 8),
        "immediate_exit_value": round(immediate_exit_value, 8) if immediate_exit_value is not None else None,
        "partial_immediate_exit_value": sweep.gross_value,
        "break_even_probability": round(break_even, 8) if break_even is not None else None,
        "unrealized_pnl": round(unrealized_pnl, 8) if unrealized_pnl is not None else None,
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 8) if unrealized_pnl_pct is not None else None,
        "last_trade_price": last_trade_price,
        "depth": depth,
        "sweep": asdict(sweep),
    }


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF Phi(x) via erf — used to size graduated de-risk trims."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def decide_position(
    *,
    marketflow_p: float | None,
    break_even_probability: float | None,
    risk_buffer: float,
    full_liquidity: bool,
    unrealized_pnl_pct: float | None = None,
    held_shares: float | None = None,
    prev_peak_price: float | None = None,
    cost_buffer: float = 0.02,
    kelly_fraction: float = 0.5,
    prob_sd: float = 0.05,
    bankroll_usd: float | None = None,
    best_ask: float | None = None,
    edge_buffer: float = 0.02,
    market_cap_frac: float = 0.40,
    min_trade_usd: float = 1.0,
    exchange_min_notional_usd: float = 5.0,
) -> dict[str, Any]:
    """Dynamic exit decision for a held binary position.

    Three regimes off the held-side win prob `marketflow_p` vs the immediate-sale
    break-even (= current sell price `b`):
      - marketflow_p BELOW b  -> de-risk. With a live position this is GRADUATED
        (TRIM_SELL_SIGNAL): trim the posterior mass that says the position is now
        -EV — trim_fraction = Phi((b - marketflow_p)/sigma) — and keep the residual as
        a model-error hedge, instead of dumping everything on one tick off a point
        estimate. Full exit (SELL_SIGNAL) only when even the ~1-sigma optimistic
        estimate is below the sale price (market clearly overpays), or there is no
        position to graduate. sigma = `prob_sd` (widen it for low-confidence p).
      - marketflow_p near b       -> DO_NOTHING.
      - marketflow_p far ABOVE b  -> underpriced, +EV. Default HOLD; but on a NEW
        favourable HIGH in the sell price (a fresh upward swing that clears cost),
        TRIM_SELL_SIGNAL: book the part of the position above a fractional-Kelly
        stake (sell into strength, convert the swing to realised PnL and cut
        binary variance), and let the +EV residual ride. The new-high gate makes
        this CONVERGENT — a position sitting at a stable price is not re-trimmed
        toward zero; only fresh upward moves bank more.
    """
    if not full_liquidity or break_even_probability is None:
        return {
            "decision": "LIQUIDITY_BLOCKED",
            "reason": "orderbook bids cannot fully liquidate configured shares; break-even threshold is not reliable",
            "risk_buffer": risk_buffer,
        }
    if marketflow_p is None:
        return {
            "decision": "OBSERVE_ONLY",
            "reason": "missing marketflow probability; provide probability_override or connect a real provider later",
            "risk_buffer": risk_buffer,
        }
    b = break_even_probability
    delta = marketflow_p - b
    base = {
        "marketflow_minus_break_even": round(delta, 8),
        "risk_buffer": risk_buffer,
        "current_sell_price": round(b, 8),
    }
    if delta < -risk_buffer:
        # Graduated de-risk: don't dump the whole position on one tick off a point
        # estimate. Trim the posterior mass that says the position is now -EV; keep
        # the residual as a model-error hedge. Full exit only when even the
        # ~1-sigma optimistic estimate is below the sale price, or when there is no
        # live position to graduate.
        sigma = max(1e-4, prob_sd)
        optimistic = marketflow_p + sigma
        if held_shares and held_shares > 0 and optimistic >= b - cost_buffer:
            trim_fraction = round(min(1.0, max(0.0, _norm_cdf((b - marketflow_p) / sigma))), 8)
            trim_shares = round(trim_fraction * held_shares, 8)
            if 0.0 < trim_fraction < 0.999 and trim_shares > 0:
                return {
                    "decision": "TRIM_SELL_SIGNAL",
                    "reason": "adverse swing; graduated partial de-risk sized by P(position is now -EV), keep the residual as a model-error hedge",
                    "keep_fraction": round(1.0 - trim_fraction, 8),
                    "trim_fraction": trim_fraction,
                    "trim_shares": trim_shares,
                    "prob_sd": round(sigma, 8),
                    **base,
                }
        return {
            "decision": "SELL_SIGNAL",
            "reason": "marketflow held-side probability below break-even minus buffer; full exit (optimistic estimate also below sale price, or no live position to graduate)",
            **base,
        }
    if delta <= risk_buffer:
        return {
            "decision": "DO_NOTHING",
            "reason": "marketflow probability is close to the immediate-sale break-even threshold",
            **base,
        }
    # delta > risk_buffer: underpriced / +EV.
    # Favourable + we know bankroll and the buy-side ask: is the fractional-Kelly
    # TARGET above what we currently hold? If the edge improved enough, scale IN
    # (add to the winner). Target-driven, NOT price-driven: if price rose but p did
    # not keep up, ask_eff rises, the target falls, and we do NOT add.
    if (bankroll_usd is not None and best_ask is not None and 0.0 < best_ask < 1.0
            and held_shares and held_shares > 0):
        sigma = max(1e-4, prob_sd)
        p_c = min(1.0 - 1e-6, max(1e-6, marketflow_p - sigma))
        ask_eff = min(1.0 - 1e-6, best_ask + cost_buffer + edge_buffer)
        f_full = max(0.0, (p_c - ask_eff) / max(1e-9, 1.0 - ask_eff)) if p_c > ask_eff else 0.0
        f_target = min(kelly_fraction * f_full, market_cap_frac)
        target_notional = f_target * bankroll_usd
        current_notional = held_shares * b
        add_notional = target_notional - current_notional
        if f_target > 0.0 and add_notional > min_trade_usd:
            # floor-to-min: add a real exchange-minimum lot to exercise the
            # entry chain; never let the resulting position exceed the market cap.
            add_notional_eff = max(add_notional, exchange_min_notional_usd)
            add_notional_eff = min(add_notional_eff, max(0.0, market_cap_frac * bankroll_usd - current_notional))
            add_shares = round(add_notional_eff / best_ask, 8)
            if add_notional_eff >= exchange_min_notional_usd - 1e-9 and add_shares > 0:
                return {
                    "decision": "SCALE_IN_SIGNAL",
                    "reason": "favourable: fractional-Kelly target rose above current holding; scale into the winner (target-driven)",
                    "target_fraction": round(f_target, 8),
                    "target_notional_usd": round(target_notional, 8),
                    "current_notional_usd": round(current_notional, 8),
                    "add_notional_usd": round(add_notional_eff, 8),
                    "add_shares": add_shares,
                    "best_ask": round(best_ask, 8),
                    **base,
                }
    # Take profit on a fresh favourable high.
    variance = b * (1.0 - b)
    new_high = prev_peak_price is None or b > prev_peak_price + cost_buffer
    in_profit = unrealized_pnl_pct is not None and unrealized_pnl_pct > cost_buffer
    if new_high and in_profit and variance > 0 and held_shares and held_shares > 0:
        keep_fraction = min(1.0, max(0.0, kelly_fraction * delta / variance))
        trim_fraction = round(1.0 - keep_fraction, 8)
        trim_shares = round(trim_fraction * held_shares, 8)
        if trim_fraction > 0 and trim_shares > 0:
            return {
                "decision": "TRIM_SELL_SIGNAL",
                "reason": "favourable swing to a new sell-price high; trim to a fractional-Kelly stake and let the +EV residual ride",
                "keep_fraction": round(keep_fraction, 8),
                "trim_fraction": trim_fraction,
                "trim_shares": trim_shares,
                "kelly_fraction": kelly_fraction,
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 8),
                **base,
            }
    return {
        "decision": "HOLD",
        "reason": "marketflow held-side probability exceeds immediate-sale break-even plus risk buffer",
        **base,
    }


def probability_panel(
    *,
    side: str,
    held_probability: float | None,
    probability_meta: dict[str, Any],
    provider_result: dict[str, Any] | None,
    market_mid: float | None,
    break_even_probability: float | None,
    decision: dict[str, Any],
) -> dict[str, Any]:
    confidence = None
    source = probability_meta.get("source") or "probability_override"
    model_id = probability_meta.get("model_id")
    model_version = probability_meta.get("model_version")
    as_of = probability_meta.get("as_of")
    calibration_status = probability_meta.get("calibration_status") or "uncalibrated"
    reject_codes: list[str] = []
    source_class = "MODEL_PROBABILITY"
    if provider_result:
        confidence = provider_result.get("confidence")
        source = provider_result.get("provider") or provider_result.get("source") or "external_win_probability_provider"
        model_id = provider_result.get("model_id") or "external_win_probability_provider"
        model_version = provider_result.get("model_version")
        as_of = provider_result.get("generated_at")
        calibration_status = provider_result.get("calibration_status") or confidence
        if confidence in NON_EDGE_CONFIDENCES:
            source_class = "NON_EDGE_REFERENCE"
            reject_codes.append("NON_EDGE_REFERENCE")
    elif held_probability is None:
        source_class = "NO_MODEL_PROBABILITY"
        reject_codes.append("MISSING_P_YES_MODEL")

    held_p = clamp_prob(held_probability)
    held_upper = side.upper()
    p_yes_model = None
    if held_p is not None and source_class == "MODEL_PROBABILITY":
        p_yes_model = held_p if held_upper == "YES" else (1.0 - held_p if held_upper == "NO" else None)

    p_market_held = clamp_prob(market_mid)
    p_market_yes = None
    if p_market_held is not None:
        p_market_yes = p_market_held if held_upper == "YES" else (1.0 - p_market_held if held_upper == "NO" else None)

    edge_held_after_cost = None
    if held_p is not None and break_even_probability is not None and source_class == "MODEL_PROBABILITY":
        edge_held_after_cost = round(held_p - break_even_probability, 8)

    # source_class (proven-edge vs market-reference) is informational metadata
    # only — it no longer vetoes the decision. The break-even decision stands;
    # `source_class` + `reject_reason_codes` are surfaced so a non-edge estimate
    # is LABELLED, not silenced. This panel is read-only either way.
    action = decision.get("decision")
    authorization = "SIGNAL_ONLY_READ_ONLY"
    return {
        "source_class": source_class,
        "p_yes_model": round(p_yes_model, 8) if p_yes_model is not None else None,
        "held_side_probability": round(held_p, 8) if held_p is not None else None,
        "probability_source": source,
        "probability_as_of": as_of,
        "model_id": model_id,
        "model_version": model_version,
        "confidence": confidence or probability_meta.get("confidence"),
        "calibration_status": calibration_status,
        "p_market_held_side": round(p_market_held, 8) if p_market_held is not None else None,
        "p_market_yes": round(p_market_yes, 8) if p_market_yes is not None else None,
        "edge_held_after_cost": edge_held_after_cost,
        "edge_yes_after_cost": edge_held_after_cost if held_upper == "YES" else None,
        "edge_no_after_cost": edge_held_after_cost if held_upper == "NO" else None,
        "selected_action": action,
        "authorization_state": authorization,
        "reject_reason_codes": reject_codes,
    }


def compact_gamma_market(market: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "conditionId",
        "questionID",
        "slug",
        "question",
        "description",
        "outcomes",
        "clobTokenIds",
        "endDate",
        "active",
        "closed",
        "archived",
        "enableOrderBook",
        "liquidityNum",
        "volumeNum",
        "volume24hr",
        "restricted",
        "negRisk",
        "umaBond",
        "umaReward",
        "resolvedBy",
    ]
    return {key: market.get(key) for key in keys if key in market}


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
    return None


def market_resolution_status(market: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    summary = config.get("_private_position_summary")
    if not isinstance(summary, dict):
        summary = {}
    redeemable = _boolish(summary.get("redeemable"))
    closed = _boolish(market.get("closed"))
    archived = _boolish(market.get("archived"))
    active = _boolish(market.get("active"))
    resolved = bool(redeemable is True or closed is True or archived is True or (active is False and closed is True))
    current_value = _rounded_float(summary.get("current_value_sdk"))
    cash_pnl = _rounded_float(summary.get("cash_pnl_sdk"))
    return {
        "resolved": resolved,
        "redeemable": redeemable,
        "gamma_closed": closed,
        "gamma_archived": archived,
        "gamma_active": active,
        "claimable_value_usd": current_value if redeemable is True else None,
        "cash_pnl_sdk": cash_pnl,
        "source": "private_position_redeemable_or_gamma_closed",
    }


def claim_redeem_result(
    config: dict[str, Any],
    *,
    market: dict[str, Any],
    token_id: str,
    token_mapping: dict[str, Any],
    side: str,
    shares: float,
    entry_cost: float | None,
    risk_buffer: float,
    settlement: dict[str, Any],
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    market_data_error: str | None = None,
    timeout_note: str | None = None,
) -> dict[str, Any]:
    bids = bids or []
    asks = asks or []
    sweep = sweep_sell_bids(bids, shares)
    depth = depth_metrics(bids, asks)
    market_slug = market.get("slug") or market_slug_from_config(config)
    market_id = market.get("conditionId") or market.get("id") or config.get("market_id")
    generated_at = iso_now()
    decision = {
        "decision": "CLAIM_REDEEM_SIGNAL",
        "reason": "market is resolved; exit path is redeem/claim rather than CLOB sell",
        "risk_buffer": risk_buffer,
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "tick_id": f"pmlpm_claim_{now_ms()}_{hash_obj({'market_id': market_id, 'token_id': token_id, 'shares': shares})[:10]}",
        "mode": "READ_ONLY_SHADOW_RUN",
        "boundaries": READ_ONLY_BOUNDARIES,
        "source_endpoints": {
            "gamma": "GET /markets metadata",
            "clob_book": "GET /book",
        },
        "config_hash": hash_obj(redact_config(config)),
        "config_redacted": redact_config(config),
        "market": {
            "market_id": str(market_id) if market_id is not None else None,
            "gamma_id": str(market.get("id")) if market.get("id") is not None else None,
            "condition_id": str(market.get("conditionId")) if market.get("conditionId") is not None else None,
            "slug": market_slug,
            "url": f"https://polymarket.com/event/{market_slug}" if market_slug else config.get("market_url"),
            "question": market.get("question"),
            "outcomes": normalize_outcomes(market),
            "raw_compact": compact_gamma_market(market),
        },
        "token": {"token_id": token_id, "mapping": token_mapping},
        "position": {
            "side": side,
            "shares": shares,
            "entry_cost": entry_cost,
            "risk_buffer": risk_buffer,
        },
        "position_source": {
            "source": config.get("_source") or "local_config",
            "entry_cost_source": config.get("_entry_cost_source"),
            "private_position_summary": config.get("_private_position_summary"),
        },
        "market_data": {
            "book_token_id": token_id,
            "bids_seen": len(bids),
            "asks_seen": len(asks),
            "orderbook_error": market_data_error,
        },
        "valuation": {
            "max_payout": round(shares, 8),
            "immediate_exit_value": None,
            "partial_immediate_exit_value": sweep.gross_value,
            "break_even_probability": None,
            "unrealized_pnl": None,
            "unrealized_pnl_pct": None,
            "last_trade_price": None,
            "depth": depth,
            "sweep": asdict(sweep),
            "settlement": settlement,
            "claimable_value_usd": settlement.get("claimable_value_usd"),
        },
        "probability": {
            "source_class": "SETTLED_MARKET",
            "selected_action": "CLAIM_REDEEM_SIGNAL",
            "authorization_state": "SIGNAL_ONLY_READ_ONLY",
            "reject_reason_codes": [],
        },
        "decision": decision,
    }
    if timeout_note:
        result["note"] = timeout_note
    return result


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _rounded_float(value: Any) -> float | None:
    f = to_float(value)
    return round(f, 8) if f is not None else None


def private_position_summary(position: Any) -> dict[str, Any]:
    """Return a ledger-safe summary of a Polymarket SDK Position."""

    size = _rounded_float(_attr(position, "size"))
    avg_price = _rounded_float(_attr(position, "avg_price"))
    initial_value = _rounded_float(_attr(position, "initial_value"))
    current_value = _rounded_float(_attr(position, "current_value"))
    cash_pnl = _rounded_float(_attr(position, "cash_pnl"))
    cur_price = _rounded_float(_attr(position, "cur_price"))
    end_date = _attr(position, "end_date")
    return {
        "condition_id": safe_str(_attr(position, "condition_id")),
        "token_id": safe_str(_attr(position, "token_id")),
        "size": size,
        "avg_price": avg_price,
        "initial_value": initial_value,
        "current_value_sdk": current_value,
        "cash_pnl_sdk": cash_pnl,
        "cur_price_sdk": cur_price,
        "title": compact_text(_attr(position, "title")),
        "slug": safe_str(_attr(position, "slug")),
        "event_id": safe_str(_attr(position, "event_id")),
        "event_slug": safe_str(_attr(position, "event_slug")),
        "outcome": safe_str(_attr(position, "outcome")),
        "outcome_index": _attr(position, "outcome_index"),
        "opposite_outcome": safe_str(_attr(position, "opposite_outcome")),
        "opposite_token_id": safe_str(_attr(position, "opposite_token_id")),
        "end_date": str(end_date) if end_date is not None else None,
        "redeemable": _attr(position, "redeemable"),
        "mergeable": _attr(position, "mergeable"),
        "negative_risk": _attr(position, "negative_risk"),
    }


def private_position_to_config(
    position: Any,
    *,
    probability_override: float | None = None,
    probability_override_side: str | None = None,
    risk_buffer: float | None = None,
) -> dict[str, Any]:
    summary = private_position_summary(position)
    shares = normalize_shares(summary.get("size"))
    token_id = safe_str(summary.get("token_id"))
    if not token_id:
        raise MonitorError("private position is missing token_id/asset")
    condition_id = safe_str(summary.get("condition_id"))
    if not condition_id:
        raise MonitorError("private position is missing condition_id")
    side = safe_str(summary.get("outcome")) or "HELD"
    entry_cost = summary.get("initial_value")
    entry_cost_source = "sdk_initial_value"
    if entry_cost is None:
        avg_price = to_float(summary.get("avg_price"))
        if avg_price is not None:
            entry_cost = round(avg_price * shares, 8)
            entry_cost_source = "sdk_avg_price_times_size"
    if entry_cost is None:
        entry_cost = summary.get("current_value_sdk")
        entry_cost_source = "sdk_current_value_fallback"

    config: dict[str, Any] = {
        "market_id": condition_id,
        "market_slug": summary.get("slug"),
        "token_id": token_id,
        "side": side,
        "shares": shares,
        "entry_cost": entry_cost,
        "_source": "polymarket_secure_client_open_position",
        "_entry_cost_source": entry_cost_source,
        "_private_position_summary": summary,
    }
    if probability_override is not None:
        config["probability_override"] = probability_override
    if probability_override_side:
        config["probability_override_side"] = probability_override_side
    if risk_buffer is not None:
        config["risk_buffer"] = risk_buffer
    return config


def build_secure_client(secrets: dict[str, str], *, secret_dir: str = DEFAULT_SECRET_DIR) -> Any:
    try:
        return pmx.build_secure_client(secrets, secret_dir=secret_dir, side="BUY")
    except pmx.ExecutionError as exc:
        raise MonitorError(str(exc)) from exc


def fetch_private_open_position_configs(
    *,
    secret_dir: str,
    page_size: int,
    max_positions: int,
    probability_override: float | None,
    probability_override_side: str | None,
    risk_buffer: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if page_size <= 0:
        raise MonitorError("private positions page_size must be positive")
    if max_positions <= 0:
        raise MonitorError("max_positions must be positive")
    secrets = load_polymarket_secret_refs(secret_dir)
    client = build_secure_client(secrets, secret_dir=secret_dir)
    positions: list[Any] = []
    pages_seen = 0
    final_has_more = False
    total_count: int | None = None
    wallet_type = None
    truncated_by_max_positions = False
    try:
        wallet_type = safe_str(client.wallet_type)
        paginator = client.list_positions(page_size=page_size)
        for page in paginator:
            pages_seen += 1
            total_count = page.total_count
            final_has_more = page.has_more
            for item in page.items:
                positions.append(item)
                if len(positions) >= max_positions:
                    truncated_by_max_positions = True
                    final_has_more = True
                    break
            if len(positions) >= max_positions or not page.has_more:
                break
    finally:
        client.close()

    configs = [
        private_position_to_config(
            position,
            probability_override=probability_override,
            probability_override_side=probability_override_side,
            risk_buffer=risk_buffer,
        )
        for position in positions
    ]
    meta = {
        "source": "polymarket_secure_client",
        "operation": "SecureClient.create + list_positions",
        "read_only": True,
        "secret_refs": secret_ref_summary(secret_dir),
        "wallet_type": wallet_type,
        "positions_count": len(configs),
        "pages_seen": pages_seen,
        "has_more": final_has_more,
        "truncated_by_max_positions": truncated_by_max_positions,
        "total_count": total_count,
        "max_positions": max_positions,
        "sdk_package": "polymarket-client",
    }
    return configs, meta


def monitor_tick(
    config: dict[str, Any],
    *,
    risk_buffer_default: float,
    timeout_note: str | None = None,
) -> dict[str, Any]:
    side = normalize_side(config.get("side"), require_binary=safe_str(config.get("token_id")) is None)
    shares = normalize_shares(config.get("shares"))
    entry_cost = normalize_entry_cost(config.get("entry_cost"))
    risk_buffer = to_float(config.get("risk_buffer"))
    if risk_buffer is None:
        risk_buffer = risk_buffer_default
    if risk_buffer < 0 or risk_buffer > 1:
        raise MonitorError("risk_buffer must be in [0,1]")
    marketflow_p, p_meta = normalize_probability_override(config, side)

    market = fetch_gamma_market(config)
    token_id, token_mapping = token_id_for_position(config, market)
    settlement = market_resolution_status(market, config)
    orderbook_error = None
    try:
        book = fetch_orderbook(token_id)
    except MonitorError as exc:
        orderbook_error = str(exc)
        if settlement["resolved"]:
            return claim_redeem_result(
                config,
                market=market,
                token_id=token_id,
                token_mapping=token_mapping,
                side=side,
                shares=shares,
                entry_cost=entry_cost,
                risk_buffer=risk_buffer,
                settlement=settlement,
                market_data_error=str(exc),
                timeout_note=timeout_note,
            )
        book = {"market": token_id, "timestamp": None, "bids": [], "asks": []}
    bids = normalize_book_levels(book, "bids")
    asks = normalize_book_levels(book, "asks")
    if not bids and settlement["resolved"]:
        return claim_redeem_result(
            config,
            market=market,
            token_id=token_id,
            token_mapping=token_mapping,
            side=side,
            shares=shares,
            entry_cost=entry_cost,
            risk_buffer=risk_buffer,
            settlement=settlement,
            bids=bids,
            asks=asks,
            timeout_note=timeout_note,
        )
    last_trade_price = None if orderbook_error else fetch_last_trade_price(token_id)
    if last_trade_price is None:
        last_trade_price = clamp_prob(book.get("last_trade_price"))

    value = compute_position_value(
        shares=shares,
        bids=bids,
        asks=asks,
        last_trade_price=last_trade_price,
        entry_cost=entry_cost,
    )
    win_prob_result = None  # set by an external win-probability provider, if any
    # Drive the decision off ANY live estimate (incl in-play low-confidence): a
    # break-even SELL must not be silenced just because the estimate is not a
    # "proven model edge". A non-edge source stays LABELLED in the probability
    # panel (source_class / reject_reason_codes) but no longer forces NONE. Only
    # a truly-blind position (marketflow_p is None) holds as OBSERVE_ONLY — and the
    # daemon alerts loudly on that instead of going silent.
    decision = decide_position(
        marketflow_p=marketflow_p,
        break_even_probability=value["break_even_probability"],
        risk_buffer=risk_buffer,
        full_liquidity=bool(value["sweep"]["full_fill"]),
        unrealized_pnl_pct=value.get("unrealized_pnl_pct"),
        held_shares=shares,
        prev_peak_price=to_float(config.get("prev_peak_price")),
        # bankroll + buy-side ask unlock target-driven scale-in (favourable add).
        # Absent -> decide_position keeps its sell-only legacy behaviour.
        bankroll_usd=to_float(config.get("kelly_bankroll_usd")),
        best_ask=to_float(value.get("depth", {}).get("best_ask")),
    )
    probability = probability_panel(
        side=side,
        held_probability=marketflow_p,
        probability_meta=p_meta,
        provider_result=win_prob_result,
        market_mid=value["depth"].get("mid"),
        break_even_probability=value["break_even_probability"],
        decision=decision,
    )
    market_slug = market.get("slug") or market_slug_from_config(config)
    market_id = market.get("conditionId") or market.get("id") or config.get("market_id")
    generated_at = iso_now()
    position = {
        "side": side,
        "shares": shares,
        "entry_cost": entry_cost,
        "probability_override": marketflow_p,
        "probability_override_meta": p_meta,
        "risk_buffer": risk_buffer,
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "tick_id": f"pmlpm_{now_ms()}_{hash_obj({'market_id': market_id, 'token_id': token_id, 'shares': shares})[:10]}",
        "mode": "READ_ONLY_SHADOW_RUN",
        "boundaries": READ_ONLY_BOUNDARIES,
        "source_endpoints": {
            "gamma": "GET /markets metadata",
            "clob_book": "GET /book",
            "clob_last_trade": "GET /last-trade-price",
        },
        "config_hash": hash_obj(redact_config(config)),
        "config_redacted": redact_config(config),
        "market": {
            "market_id": str(market_id) if market_id is not None else None,
            "gamma_id": str(market.get("id")) if market.get("id") is not None else None,
            "condition_id": str(market.get("conditionId")) if market.get("conditionId") is not None else None,
            "slug": market_slug,
            "url": f"https://polymarket.com/event/{market_slug}" if market_slug else config.get("market_url"),
            "question": market.get("question"),
            "outcomes": normalize_outcomes(market),
            "raw_compact": compact_gamma_market(market),
        },
        "token": {
            "token_id": token_id,
            "mapping": token_mapping,
        },
        "position": position,
        "win_probability_provider": win_prob_result,
        "probability": probability,
        "position_source": {
            "source": config.get("_source") or "local_config",
            "entry_cost_source": config.get("_entry_cost_source"),
            "private_position_summary": config.get("_private_position_summary"),
        },
        "market_data": {
            "book_token_id": safe_str(book.get("market")) or token_id,
            "book_timestamp": book.get("timestamp"),
            "bids_seen": len(bids),
            "asks_seen": len(asks),
            "orderbook_error": orderbook_error,
        },
        "valuation": value,
        "decision": decision,
    }
    if timeout_note:
        result["note"] = timeout_note
    return result


def human_summary(result: dict[str, Any]) -> str:
    if isinstance(result.get("positions"), list):
        return human_account_summary(result)
    market = result.get("market", {})
    pos = result.get("position", {})
    val = result.get("valuation", {})
    depth = val.get("depth", {})
    sweep = val.get("sweep", {})
    dec = result.get("decision", {})
    prob = result.get("probability", {})
    lines = [
        "# Polymarket Live Position Monitor v0.1",
        "",
        f"- generated_at: `{result.get('generated_at')}`",
        f"- mode: `{result.get('mode')}`",
        f"- decision: `{dec.get('decision')}`",
        f"- reason: {dec.get('reason')}",
        "",
        "## Position",
        "",
        f"- market: {market.get('question')}",
        f"- slug: `{market.get('slug')}`",
        f"- side: `{pos.get('side')}`",
        f"- shares: `{pos.get('shares')}`",
        f"- token_id: `{result.get('token', {}).get('token_id')}`",
        "",
        "## Immediate Sell Estimate",
        "",
        f"- max_payout: `{val.get('max_payout')}`",
        f"- immediate_exit_value: `{val.get('immediate_exit_value')}`",
        f"- partial_immediate_exit_value: `{val.get('partial_immediate_exit_value')}`",
        f"- break_even_probability: `{val.get('break_even_probability')}`",
        f"- p_yes_model: `{prob.get('p_yes_model')}`",
        f"- held_side_probability: `{prob.get('held_side_probability')}`",
        f"- probability_source: `{prob.get('probability_source')}` / `{prob.get('source_class')}`",
        f"- model_version: `{prob.get('model_version')}`",
        f"- authorization_state: `{prob.get('authorization_state')}`",
        f"- best_bid / best_ask: `{depth.get('best_bid')}` / `{depth.get('best_ask')}`",
        f"- spread: `{depth.get('spread')}`",
        f"- average_sell_price: `{sweep.get('average_fill_price')}`",
        f"- slippage_vs_best_bid: `{sweep.get('slippage_vs_best_bid')}`",
        f"- full_liquidity: `{sweep.get('full_fill')}`",
        f"- unfilled_shares: `{sweep.get('unfilled_shares')}`",
    ]
    if val.get("unrealized_pnl") is not None:
        lines.extend([
            f"- unrealized_pnl: `{val.get('unrealized_pnl')}`",
            f"- unrealized_pnl_pct: `{val.get('unrealized_pnl_pct')}`",
        ])
    lines.extend([
        "",
        "## Boundary",
        "",
        "Read-only shadow run. No orders, cancels, wallet reads, private keys, API secrets or seed phrases.",
        "",
    ])
    return "\n".join(lines)


def human_account_summary(result: dict[str, Any]) -> str:
    account = result.get("account", {})
    dec = result.get("decision", {})
    positions = result.get("positions", [])
    lines = [
        "# Polymarket Live Position Monitor v0.1",
        "",
        f"- generated_at: `{result.get('generated_at')}`",
        f"- mode: `{result.get('mode')}`",
        f"- account_decision: `{dec.get('decision')}`",
        f"- reason: {dec.get('reason')}",
        f"- wallet_type: `{account.get('wallet_type')}`",
        f"- positions_count: `{account.get('positions_count')}`",
        f"- pages_seen: `{account.get('pages_seen')}`",
        f"- has_more: `{account.get('has_more')}`",
        "",
        "## Positions",
        "",
    ]
    if not positions:
        lines.append("- no open positions returned")
    for idx, item in enumerate(positions, start=1):
        market = item.get("market", {})
        pos = item.get("position", {})
        val = item.get("valuation", {})
        sweep = val.get("sweep", {})
        item_dec = item.get("decision", {})
        prob = item.get("probability", {})
        lines.extend([
            f"### {idx}. {market.get('question') or market.get('slug') or 'position'}",
            "",
            f"- decision: `{item_dec.get('decision')}`",
            f"- side: `{pos.get('side')}`",
            f"- shares: `{pos.get('shares')}`",
            f"- immediate_exit_value: `{val.get('immediate_exit_value')}`",
            f"- partial_immediate_exit_value: `{val.get('partial_immediate_exit_value')}`",
            f"- break_even_probability: `{val.get('break_even_probability')}`",
            f"- probability_source: `{prob.get('probability_source')}` / `{prob.get('source_class')}`",
            f"- unrealized_pnl: `{val.get('unrealized_pnl')}`",
            f"- average_sell_price: `{sweep.get('average_fill_price')}`",
            f"- full_liquidity: `{sweep.get('full_fill')}`",
            f"- token_id: `{item.get('token', {}).get('token_id')}`",
            "",
        ])
    lines.extend([
        "## Boundary",
        "",
        "Read-only account position discovery plus public market-data reads. No orders, cancels, secret printing, or secret ledger writes.",
        "",
    ])
    return "\n".join(lines)


def error_record(exc: Exception, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "mode": "READ_ONLY_SHADOW_RUN",
        "boundaries": READ_ONLY_BOUNDARIES,
        "decision": {
            "decision": "MARKET_DATA_BLOCKED",
            "reason": str(exc),
        },
        "config_redacted": redact_config(config or {}),
        "config_hash": hash_obj(redact_config(config or {})),
    }


def aggregate_decision(position_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not position_results:
        return {
            "decision": "OBSERVE_ONLY",
            "reason": "authenticated account read succeeded and returned no open positions",
        }
    counts: dict[str, int] = {}
    for result in position_results:
        key = str(result.get("decision", {}).get("decision") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    if any(key in QUALITY_FAIL_DECISIONS for key in counts):
        decision = "MARKET_DATA_BLOCKED"
        reason = "one or more open positions could not be fully valued from read-only market data"
    elif counts.get("CLAIM_REDEEM_SIGNAL"):
        decision = "CLAIM_REDEEM_SIGNAL"
        reason = "at least one resolved position should exit via redeem/claim rather than CLOB sell"
    elif counts.get("SELL_SIGNAL"):
        decision = "SELL_SIGNAL"
        reason = "at least one open position is below the immediate-sale break-even threshold"
    elif counts.get("LIQUIDITY_BLOCKED"):
        decision = "LIQUIDITY_BLOCKED"
        reason = "at least one unresolved position has insufficient CLOB bids for a full mechanical sell"
    elif counts.get("HOLD") and counts.get("HOLD") == len(position_results):
        decision = "HOLD"
        reason = "all open positions with probability inputs clear the hold threshold"
    elif counts.get("DO_NOTHING"):
        decision = "DO_NOTHING"
        reason = "at least one position is near break-even within the configured risk buffer"
    else:
        decision = "OBSERVE_ONLY"
        reason = "positions valued successfully but no MarketFlow probability input was supplied"
    return {"decision": decision, "reason": reason, "position_decision_counts": counts}


def account_monitor_record(account_meta: dict[str, Any], position_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "tick_id": f"pmlpm_account_{now_ms()}_{hash_obj(account_meta)[:10]}",
        "mode": "READ_ONLY_LIVE_ACCOUNT_MONITOR",
        "boundaries": READ_ONLY_BOUNDARIES,
        "source_endpoints": {
            "private_positions": "SecureClient.create + GET data positions via SDK",
            "gamma": "GET /markets metadata",
            "clob_book": "GET /book",
            "clob_last_trade": "GET /last-trade-price",
        },
        "account": account_meta,
        "positions": position_results,
        "decision": aggregate_decision(position_results),
    }


def account_error_record(exc: Exception, *, secret_dir: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "mode": "READ_ONLY_LIVE_ACCOUNT_MONITOR",
        "boundaries": READ_ONLY_BOUNDARIES,
        "account": {
            "source": "polymarket_secure_client",
            "operation": "SecureClient.create + list_positions",
            "read_only": True,
            "secret_refs": secret_ref_summary(secret_dir),
        },
        "positions": [],
        "decision": {
            "decision": "MARKET_DATA_BLOCKED",
            "reason": str(exc),
        },
    }


def run_monitor(args: argparse.Namespace) -> int:
    config = load_position_config(args.config)
    ticks_done = 0
    final_result: dict[str, Any] | None = None
    while True:
        try:
            result = monitor_tick(config, risk_buffer_default=args.risk_buffer)
        except Exception as exc:
            result = error_record(exc, config)
        append_jsonl(args.ledger, result)
        write_json(args.latest_json, result)
        ensure_parent(args.latest_summary)
        with open(args.latest_summary, "w", encoding="utf-8") as f:
            f.write(human_summary(result))
        final_result = result
        ticks_done += 1
        if args.print_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(f"{result['generated_at']} {result['decision']['decision']} {result['decision'].get('reason')}")

        if args.ticks > 0 and ticks_done >= args.ticks:
            break
        if args.once:
            break
        if args.ticks == 0 or ticks_done < args.ticks:
            time.sleep(args.interval)

    if final_result and final_result.get("decision", {}).get("decision") in QUALITY_FAIL_DECISIONS:
        return 2
    return 0


def run_live_account_monitor(args: argparse.Namespace) -> int:
    probability_override = None
    if args.probability_override is not None:
        probability_override = clamp_prob(args.probability_override)
        if probability_override is None:
            print("error: --probability-override must be a probability in [0,1] or percent in (1,100]", file=sys.stderr)
            return 2

    ticks_done = 0
    final_result: dict[str, Any] | None = None
    while True:
        try:
            configs, account_meta = fetch_private_open_position_configs(
                secret_dir=args.secret_dir,
                page_size=args.page_size,
                max_positions=args.max_positions,
                probability_override=probability_override,
                probability_override_side=args.probability_override_side,
                risk_buffer=args.risk_buffer,
            )
            position_results: list[dict[str, Any]] = []
            for config in configs:
                try:
                    position_results.append(
                        monitor_tick(config, risk_buffer_default=args.risk_buffer)
                    )
                except Exception as exc:
                    err = error_record(exc, config)
                    err["position_source"] = {
                        "source": config.get("_source"),
                        "entry_cost_source": config.get("_entry_cost_source"),
                        "private_position_summary": config.get("_private_position_summary"),
                    }
                    position_results.append(err)
            result = account_monitor_record(account_meta, position_results)
        except Exception as exc:
            result = account_error_record(exc, secret_dir=args.secret_dir)

        append_jsonl(args.ledger, result)
        write_json(args.latest_json, result)
        ensure_parent(args.latest_summary)
        with open(args.latest_summary, "w", encoding="utf-8") as f:
            f.write(human_summary(result))
        final_result = result
        ticks_done += 1

        if args.print_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            dec = result["decision"]
            count = result.get("account", {}).get("positions_count", len(result.get("positions", [])))
            print(f"{result['generated_at']} {dec['decision']} positions={count} {dec.get('reason')}")

        if args.ticks > 0 and ticks_done >= args.ticks:
            break
        if args.once:
            break
        if args.ticks == 0 or ticks_done < args.ticks:
            time.sleep(args.interval)

    if final_result and final_result.get("decision", {}).get("decision") in QUALITY_FAIL_DECISIONS:
        return 2
    return 0


def assert_true(checks: dict[str, bool], key: str, value: bool) -> None:
    checks[key] = bool(value)


def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    bids = [(0.60, 10.0), (0.58, 10.0), (0.55, 10.0)]
    asks = [(0.63, 5.0), (0.65, 10.0)]
    val = compute_position_value(shares=25.0, bids=bids, asks=asks, last_trade_price=0.61, entry_cost=12.5)
    assert_true(checks, "synthetic_orderbook_sweep_full_fill", val["sweep"]["full_fill"])
    assert_true(checks, "synthetic_orderbook_sweep_value", abs(val["immediate_exit_value"] - 14.55) < 1e-9)
    assert_true(checks, "break_even_probability", abs(val["break_even_probability"] - 0.582) < 1e-9)
    assert_true(checks, "spread_depth", val["depth"]["spread"] == 0.03 and val["depth"]["bid_depth_shares"] == 30.0)
    assert_true(checks, "unrealized_pnl", abs(val["unrealized_pnl"] - 2.05) < 1e-9)

    hold = decide_position(marketflow_p=0.65, break_even_probability=0.58, risk_buffer=0.03, full_liquidity=True)
    sell = decide_position(marketflow_p=0.52, break_even_probability=0.58, risk_buffer=0.03, full_liquidity=True)
    observe = decide_position(marketflow_p=None, break_even_probability=0.58, risk_buffer=0.03, full_liquidity=True)
    low = decide_position(marketflow_p=0.595, break_even_probability=0.58, risk_buffer=0.03, full_liquidity=True)
    assert_true(checks, "decision_hold", hold["decision"] == "HOLD")
    assert_true(checks, "decision_sell", sell["decision"] == "SELL_SIGNAL")
    assert_true(checks, "decision_observe_only", observe["decision"] == "OBSERVE_ONLY")
    assert_true(checks, "decision_do_nothing", low["decision"] == "DO_NOTHING")

    # adverse WITH a live position: graduated partial de-risk, not all-or-nothing.
    grad = decide_position(marketflow_p=0.52, break_even_probability=0.58, risk_buffer=0.03,
                           full_liquidity=True, held_shares=100.0, prob_sd=0.05)
    assert_true(checks, "adverse_graduated_partial_trim", grad["decision"] == "TRIM_SELL_SIGNAL")
    assert_true(checks, "adverse_keeps_residual_hedge", 0.0 < grad["trim_shares"] < 100.0)
    # deep adverse (even optimistic estimate below sale price): full exit.
    deep = decide_position(marketflow_p=0.40, break_even_probability=0.58, risk_buffer=0.03,
                           full_liquidity=True, held_shares=100.0, prob_sd=0.05)
    assert_true(checks, "deep_adverse_full_exit", deep["decision"] == "SELL_SIGNAL")
    # no live position to graduate: stays a pure SELL_SIGNAL (back-compat).
    assert_true(checks, "adverse_no_position_signal_only", sell["decision"] == "SELL_SIGNAL")

    # favourable + edge improved + we know bankroll/ask -> scale IN (add to winner).
    scin = decide_position(marketflow_p=0.85, break_even_probability=0.55, risk_buffer=0.03,
                           full_liquidity=True, held_shares=5.0, bankroll_usd=100.0,
                           best_ask=0.56, prob_sd=0.05)
    assert_true(checks, "favourable_edge_up_scales_in", scin["decision"] == "SCALE_IN_SIGNAL")
    assert_true(checks, "scale_in_adds_shares", scin.get("add_shares", 0) > 0)
    # price up but p did NOT keep up -> target-driven: ask_eff exceeds p_c -> NO add.
    no_add = decide_position(marketflow_p=0.62, break_even_probability=0.61, risk_buffer=0.03,
                             full_liquidity=True, held_shares=5.0, bankroll_usd=100.0,
                             best_ask=0.62, prob_sd=0.05)
    assert_true(checks, "price_up_p_flat_no_scale_in", no_add["decision"] != "SCALE_IN_SIGNAL")
    # back-compat: favourable WITHOUT bankroll/ask -> never scales in (HOLD).
    fav_legacy = decide_position(marketflow_p=0.85, break_even_probability=0.55, risk_buffer=0.03,
                                 full_liquidity=True, held_shares=5.0)
    assert_true(checks, "favourable_no_bankroll_legacy_hold", fav_legacy["decision"] in ("HOLD", "TRIM_SELL_SIGNAL"))

    # dynamic take-profit: a fresh favourable high trims to a fractional-Kelly stake
    trim = decide_position(
        marketflow_p=0.88, break_even_probability=0.82, risk_buffer=0.03, full_liquidity=True,
        unrealized_pnl_pct=0.26, held_shares=100.0, prev_peak_price=None,
    )
    assert_true(checks, "take_profit_trims_on_new_high", trim["decision"] == "TRIM_SELL_SIGNAL")
    _kf = min(1.0, max(0.0, 0.5 * (0.88 - 0.82) / (0.82 * (1 - 0.82))))
    _trimf = round(1.0 - _kf, 8)
    assert_true(checks, "take_profit_kelly_keep_fraction", abs(trim["keep_fraction"] - round(_kf, 8)) < 1e-9)
    assert_true(checks, "take_profit_trim_shares", abs(trim["trim_shares"] - round(_trimf * 100.0, 8)) < 1e-9)
    # convergence: a stable price (no new high beyond cost) is NOT re-trimmed toward zero
    no_retrim = decide_position(
        marketflow_p=0.88, break_even_probability=0.82, risk_buffer=0.03, full_liquidity=True,
        unrealized_pnl_pct=0.26, held_shares=100.0, prev_peak_price=0.82,
    )
    assert_true(checks, "take_profit_no_retrim_stable_price", no_retrim["decision"] == "HOLD")
    # a flat position (no realised favourable swing) holds full, never trims
    flat = decide_position(
        marketflow_p=0.88, break_even_probability=0.82, risk_buffer=0.03, full_liquidity=True,
        unrealized_pnl_pct=0.0, held_shares=100.0, prev_peak_price=None,
    )
    assert_true(checks, "take_profit_flat_holds_full", flat["decision"] == "HOLD")
    # a fresh HIGHER high (price jumped further on another event) trims again
    higher = decide_position(
        marketflow_p=0.95, break_even_probability=0.90, risk_buffer=0.03, full_liquidity=True,
        unrealized_pnl_pct=0.38, held_shares=20.0, prev_peak_price=0.82,
    )
    assert_true(checks, "take_profit_retrims_on_higher_high", higher["decision"] == "TRIM_SELL_SIGNAL")

    thin_val = compute_position_value(shares=25.0, bids=[(0.50, 5.0)], asks=[], last_trade_price=None, entry_cost=None)
    blocked = decide_position(
        marketflow_p=0.90,
        break_even_probability=thin_val["break_even_probability"],
        risk_buffer=0.03,
        full_liquidity=thin_val["sweep"]["full_fill"],
    )
    assert_true(checks, "missing_liquidity_fail_loud", blocked["decision"] == "LIQUIDITY_BLOCKED")
    assert_true(checks, "partial_immediate_exit_preserved", thin_val["partial_immediate_exit_value"] == 2.5)
    assert_true(checks, "full_immediate_exit_value_missing_when_thin", thin_val["immediate_exit_value"] is None)

    non_edge_panel = probability_panel(
        side="YES",
        held_probability=0.55,
        probability_meta={"source": "external_win_probability_provider"},
        provider_result={"provider": "fixture", "confidence": "pre_match_market", "model_version": "x"},
        market_mid=0.55,
        break_even_probability=0.50,
        decision=hold,
    )
    # Recalibrated: a non-edge source is LABELLED, not vetoed — the break-even
    # decision passes through; source_class + reject codes stay as metadata.
    assert_true(checks, "non_edge_reference_action_passthrough", non_edge_panel["selected_action"] == hold["decision"])
    assert_true(checks, "non_edge_reference_not_vetoed", non_edge_panel["authorization_state"] == "SIGNAL_ONLY_READ_ONLY")
    assert_true(checks, "non_edge_reference_tagged", non_edge_panel["source_class"] == "NON_EDGE_REFERENCE" and "NON_EDGE_REFERENCE" in non_edge_panel["reject_reason_codes"])

    bad_secret = False
    try:
        validate_no_sensitive_config({"api_secret": "do-not-use"})
    except MonitorError:
        bad_secret = True
    assert_true(checks, "sensitive_config_rejected", bad_secret)

    explicit_token_market = {"outcomes": ["Team A", "Team B"], "clobTokenIds": ["1", "2"]}
    explicit_token, explicit_mapping = token_id_for_position(
        {"token_id": "abc", "side": "Team A"}, explicit_token_market
    )
    assert_true(checks, "explicit_token_allows_non_binary_outcome", explicit_token == "abc")
    assert_true(checks, "explicit_token_mapping_source", explicit_mapping["source"] == "config.token_id")

    class FakePrivatePosition:
        condition_id = "0x" + "1" * 64
        token_id = "123456789"
        size = Decimal("10.5")
        avg_price = Decimal("0.42")
        initial_value = Decimal("4.41")
        current_value = Decimal("5.00")
        cash_pnl = Decimal("0.59")
        cur_price = Decimal("0.50")
        title = "Synthetic open position"
        slug = "synthetic-market"
        event_id = "100"
        event_slug = "synthetic-event"
        outcome = "Team A"
        outcome_index = 0
        opposite_outcome = "Team B"
        opposite_token_id = "987654321"
        end_date = None
        redeemable = False
        mergeable = False
        negative_risk = False

    private_cfg = private_position_to_config(FakePrivatePosition(), risk_buffer=0.02)
    assert_true(checks, "private_position_to_config_shares", private_cfg["shares"] == 10.5)
    assert_true(checks, "private_position_to_config_entry_cost", private_cfg["entry_cost"] == 4.41)
    assert_true(checks, "private_position_to_config_source", private_cfg["_source"] == "polymarket_secure_client_open_position")
    resolved_cfg = dict(private_cfg)
    resolved_cfg["_private_position_summary"] = dict(private_cfg["_private_position_summary"], redeemable=True, current_value_sdk=0.0)
    resolved_market = {"conditionId": "0x" + "1" * 64, "slug": "synthetic-resolved", "closed": True, "active": False}
    settlement = market_resolution_status(resolved_market, resolved_cfg)
    claim_rec = claim_redeem_result(
        resolved_cfg,
        market=resolved_market,
        token_id="123456789",
        token_mapping={"source": "config.token_id"},
        side="Team A",
        shares=10.5,
        entry_cost=4.41,
        risk_buffer=0.02,
        settlement=settlement,
        bids=[],
        asks=[],
        market_data_error="GET /book: HTTP 404",
    )
    assert_true(checks, "resolved_redeemable_detected", settlement["resolved"] is True and settlement["redeemable"] is True)
    assert_true(checks, "resolved_no_bids_claim_signal", claim_rec["decision"]["decision"] == "CLAIM_REDEEM_SIGNAL")
    assert_true(checks, "claim_signal_keeps_loop_non_error", claim_rec["decision"]["decision"] not in QUALITY_FAIL_DECISIONS)
    agg_claim = aggregate_decision([claim_rec])
    assert_true(checks, "aggregate_claim_redeem_signal", agg_claim["decision"] == "CLAIM_REDEEM_SIGNAL")
    agg_liq = aggregate_decision([{"decision": {"decision": "LIQUIDITY_BLOCKED"}}])
    assert_true(checks, "aggregate_liquidity_not_market_data_blocked", agg_liq["decision"] == "LIQUIDITY_BLOCKED")

    ok = all(checks.values())
    report = {
        "schema_version": "polymarket-live-position-monitor-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": ok,
        "checks": checks,
        "fixtures": {
            "full_fill_value": val,
            "thin_liquidity_value": thin_val,
            "decisions": {
                "hold": hold,
                "sell": sell,
                "observe": observe,
                "do_nothing": low,
                "liquidity_blocked": blocked,
            },
        },
    }
    write_json(DEFAULT_SELFTEST, report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only Polymarket live position monitor v0.1.")
    p.add_argument("--config", help="Local JSON position config.")
    p.add_argument("--live-account", action="store_true", help="Read current open positions from local Polymarket secret refs.")
    p.add_argument("--secret-dir", default=DEFAULT_SECRET_DIR, help="Directory containing Polymarket secret ref files.")
    p.add_argument("--page-size", type=int, default=20, help="Private positions page size for --live-account.")
    p.add_argument("--max-positions", type=int, default=25, help="Maximum private open positions to value per tick.")
    p.add_argument("--probability-override", type=float, help="Held-side payout probability override for live-account positions.")
    p.add_argument("--probability-override-side", help="HELD, YES, NO, or held outcome label for --probability-override.")
    p.add_argument("--risk-buffer", type=float, default=0.03, help="Probability margin around break-even threshold.")
    p.add_argument("--interval", type=float, default=5.0, help="Seconds between ticks when not --once.")
    p.add_argument("--ticks", type=int, default=1, help="Number of ticks; 0 means run until interrupted.")
    p.add_argument("--once", action="store_true", help="Run one tick regardless of --ticks.")
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--latest-json", default=DEFAULT_LATEST_JSON)
    p.add_argument("--latest-summary", default=DEFAULT_LATEST_SUMMARY)
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["PASS"] else 1
    if args.live_account and args.config:
        print("error: use either --live-account or --config, not both", file=sys.stderr)
        return 2
    if not args.live_account and not args.config:
        print("error: --config or --live-account is required unless --selftest is used", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("error: --interval must be positive", file=sys.stderr)
        return 2
    if args.ticks < 0:
        print("error: --ticks must be >= 0", file=sys.stderr)
        return 2
    if args.page_size <= 0:
        print("error: --page-size must be positive", file=sys.stderr)
        return 2
    if args.max_positions <= 0:
        print("error: --max-positions must be positive", file=sys.stderr)
        return 2
    if args.live_account:
        return run_live_account_monitor(args)
    return run_monitor(args)


if __name__ == "__main__":
    raise SystemExit(main())
