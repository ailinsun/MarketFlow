"""Alert rule engine (pure functions).

Input: one normalised position, that position's rolling high, an optional
settlement assessment, configured thresholds, and the current time.
Output: a list of candidate alerts, each with one sentence of plain-language
reasoning.

The rules. The three price rules need nothing but public position data. The three
settlement rules come from the settlement guard, which consumes market metadata
and on-chain oracle state. The flow rule is a read-only structural addition.

  1. drawdown_from_entry   — current price well below the entry price
  2. drawdown_from_high    — current price well below the high seen since watching
  3. settlement_window     — banded settlement warnings (T-48h / T-24h / T-2h),
                             carrying verbatim clauses from the resolution text
                             that may diverge from the headline. Nothing is
                             generated when nothing can be extracted.
  4. uma_activity          — the market entered oracle proposal or dispute, with
                             the proposed outcome against the held side. When the
                             on-chain direction cannot be read, it says so.
  5. settlement_mispriced  — the proposed outcome diverges sharply from the market
                             price: the shape of a mispriced resolution.
  6. new_position          — a newly opened position was detected (informational)
  7. large_print_flow      — aggregated large-print structure over a short window.
                             It describes trades and predicts nothing.

All three settlement verdicts live in the settlement guard. This module only
turns an assessment into alert text and a de-duplication bucket.

De-duplication: every alert carries a stable scope plus a severity bucket. The
same scope only fires again when the bucket worsens, so a polling loop cannot
spam. The bookkeeping helpers should_fire and mark_fired are here too; they
operate in place on the fired dict and never touch disk.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The upstream filename is kept for compatibility with a running collector. At
# this layer it is simply a raw large-print stream and carries no skill label of
# any kind. It may not exist: a stopped or not-yet-populated feed silences the
# rule entirely rather than erroring.
LARGE_PRINT_FEED_PATH = os.environ.get("MARKETFLOW_WHALE_FEED") or os.path.join(
    _PROJECT_DIR, "runtime", "feeds", "smart_money", "whale_trades.jsonl"
)


@dataclass
class AlertConfig:
    # fire when the current price is this far below entry (0.25 = down 25%)
    entry_drawdown: float = 0.25
    # fire when the current price is this far below the high seen since watching
    high_drawdown: float = 0.20
    # settlement window bands in hours, widest first (bucket 1..n), plus the final band
    settlement_window_hours: tuple[float, ...] = (48.0, 24.0)
    final_resolution_hours: float = 2.0
    # proposed side priced below this raises a mispriced-settlement warning
    mispriced_price_floor: float = 0.85
    mispriced_bucket_step: float = 0.05
    # positions below this notional are not alerted on (noise control); the larger
    # of cost basis and current value is used
    min_notional_usd: float = 5.0
    # bucket step for drawdown alerts: re-alert every further 10% of deterioration
    drawdown_bucket_step: float = 0.10
    enable_new_position: bool = True
    # price-jump attribution: fire when |delta price| in the window reaches this
    # many probability points
    price_jump_pts: float = 0.05
    price_jump_window_sec: float = 1800.0
    # cooldown between jump alerts on one position: repeat-event de-duplication
    # rather than the bucket-worsening kind
    price_jump_cooldown_sec: float = 3600.0
    enable_price_jump: bool = True
    # aggregated large prints: only trades at or above this notional enter the
    # structural summary
    large_print_min_notional_usd: float = 5000.0
    large_print_window_sec: float = 900.0
    large_print_cooldown_sec: float = 7200.0
    enable_large_print_flow: bool = True


def _notional(pos: dict[str, Any]) -> float:
    """Notional size of a position in USD: the larger of cost basis and current
    value, falling back to size * avgPrice.

    The larger is used so that a position which was bought and has since gone to
    zero — exactly the one most worth alerting on — is not filtered out by a
    current value of approximately nothing."""
    initial = pos.get("initialValue") or 0.0
    current = pos.get("currentValue") or 0.0
    n = max(float(initial), float(current))
    if n <= 0:
        size = pos.get("size") or 0.0
        avg = pos.get("avgPrice") or 0.0
        n = float(size) * float(avg)
    return n


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "?"
    return f"${p:.2f}"


def is_price_active(pos: dict[str, Any], *, now: datetime) -> bool:
    """Whether this position is still a live price target — the gate in front of
    every price rule (drawdown, pullback, jump, new position).

    A position is dead on two hard facts only: it is settled and redeemable, or
    its market is past its end date. Such a position's price will never move
    again, so an alert about it carries no action. Without this gate, watching a
    new address for the first time buries the reader in dead history.

    **Deadness is never judged by price.** A live market collapsing toward zero is
    precisely the event most worth reporting; silencing historical zeros is the job
    of first-run seeding, not of this function.

    Settlement and oracle rules do not pass through this gate: a market past its
    end date and still unresolved is exactly where the settlement guard earns its
    keep."""
    if pos.get("redeemable"):
        return False
    end_dt = _parse_iso(pos.get("endDate") or "")
    if end_dt is not None and end_dt < now:
        return False
    return True


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    s = ts.strip().replace("Z", "+00:00")
    for parse in (
        lambda x: datetime.fromisoformat(x),
        lambda x: datetime.strptime(x, "%Y-%m-%d").replace(tzinfo=timezone.utc),
    ):
        try:
            dt = parse(s if "T" in s else ts.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------- large-print flow source


def load_large_print_flow_index(
    condition_ids: set[str],
    *,
    now: datetime | None = None,
    config: "AlertConfig | None" = None,
    path: str = LARGE_PRINT_FEED_PATH,
    tail_bytes: int = 512 * 1024,
) -> dict[str, dict[str, Any]]:
    """Read the tail of the raw trade feed; returns {conditionId: short-window
    aggregate}.

    The feed is append-only jsonl and can run to tens of thousands of lines, so
    only the last tail_bytes are seeked and read. Rows are filtered by source
    timestamp, notional threshold and condition id, aggregated per market by side
    and outcome, and de-duplicated by transaction hash plus asset, side and size.

    The output describes observed trade structure. It infers nothing about wallet
    quality and nothing about future direction. A missing file, a malformed line
    or a row with no timestamp is skipped silently; this never raises."""
    cfg = config or AlertConfig()
    now = now or datetime.now(timezone.utc)
    if not condition_ids or not os.path.exists(path):
        return {}
    wanted = {str(c).strip().lower() for c in condition_ids if c}
    floor_ms = (now.timestamp() - cfg.large_print_window_sec) * 1000.0
    grouped: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = chunk.splitlines()
    if len(chunk) >= tail_bytes:
        lines = lines[1:]  # the first line may be cut in half
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("signal_type") != "whale_print":
            continue
        try:
            ts_ms = float(rec.get("ts_source_ms") or 0)
            notional = float(rec.get("notional_usd") or 0)
        except (TypeError, ValueError):
            continue
        if ts_ms < floor_ms or notional < cfg.large_print_min_notional_usd:
            continue
        cid = str(rec.get("condition_id") or "").strip().lower()
        if cid not in wanted:
            continue
        dedup = (
            str(rec.get("tx_hash") or rec.get("ts_source_ms") or ""),
            str(rec.get("asset") or ""),
            str(rec.get("side") or "").upper(),
            str(rec.get("size") or rec.get("notional_usd") or ""),
        )
        if dedup in seen:
            continue
        seen.add(dedup)
        side = str(rec.get("side") or "?").upper()
        outcome = str(rec.get("outcome") or "?").strip() or "?"
        leg = f"{side}|{outcome}"
        g = grouped.setdefault(cid, {
            "condition_id": cid,
            "title": str(rec.get("title") or ""),
            "print_count": 0,
            "notional_usd": 0.0,
            "wallets": set(),
            "legs": {},
            "largest_print": rec,
            "first_ts_ms": ts_ms,
            "last_ts_ms": ts_ms,
        })
        g["print_count"] += 1
        g["notional_usd"] += notional
        wallet = str(rec.get("wallet") or "").strip().lower()
        if wallet:
            g["wallets"].add(wallet)
        g["legs"][leg] = float(g["legs"].get(leg, 0.0)) + notional
        g["first_ts_ms"] = min(float(g["first_ts_ms"]), ts_ms)
        g["last_ts_ms"] = max(float(g["last_ts_ms"]), ts_ms)
        if notional > float(g["largest_print"].get("notional_usd") or 0):
            g["largest_print"] = rec
    out: dict[str, dict[str, Any]] = {}
    for cid, g in grouped.items():
        dominant_leg, dominant_notional = max(g["legs"].items(), key=lambda kv: kv[1])
        side, outcome = dominant_leg.split("|", 1)
        total = float(g["notional_usd"])
        out[cid] = {
            **{k: v for k, v in g.items() if k not in ("wallets", "legs")},
            "notional_usd": round(total, 2),
            "wallet_count": len(g["wallets"]),
            "dominant_side": side,
            "dominant_outcome": outcome,
            "dominant_notional_usd": round(float(dominant_notional), 2),
            "dominant_share": (float(dominant_notional) / total) if total > 0 else 0.0,
        }
    return out


def large_print_flow_alert(
    pos: dict[str, Any], flow: dict[str, Any], *, config: "AlertConfig | None" = None,
) -> dict[str, Any]:
    """Turn a short-window flow aggregate into a neutral structural alert. It
    attributes no predictive skill to whoever traded."""
    cfg = config or AlertConfig()
    title = str(pos.get("title") or flow.get("title") or "")[:80]
    side = str(flow.get("dominant_side") or "?").upper()
    outcome = str(flow.get("dominant_outcome") or "?")
    notional = float(flow.get("notional_usd") or 0)
    count = int(flow.get("print_count") or 0)
    wallets = int(flow.get("wallet_count") or 0)
    share = float(flow.get("dominant_share") or 0)
    side_word = "buying" if side == "BUY" else "selling"
    return {
        "rule": "large_print_flow",
        "scope": f"large_print_flow|{str(flow.get('condition_id') or '')[:24]}|{side}|{outcome}",
        "bucket": 1,
        "cooldown_sec": cfg.large_print_cooldown_sec,
        "severity": "info",
        "emoji": "🌊",
        "title": title,
        "reason": f"🌊 Large-print flow: {title} — {count} prints above "
            f"${cfg.large_print_min_notional_usd:,.0f} in the last {cfg.large_print_window_sec/60:.0f}m, "
            f"${notional:,.0f} total across {wallets} wallets; {share*100:.0f}% concentrated in "
            f"{side} {outcome}. This describes traded flow, not a forecast.",
    }


def signal_metadata(alert: dict[str, Any]) -> dict[str, Any]:
    """A stable semantic layer over detected events. Detectors produce facts; a
    presentation layer may explain them and may never invent a direction."""
    rule = str(alert.get("rule") or "")
    kind = {
        "large_print_flow": "market_flow",
        "price_jump": "market_repricing",
        "market_jump": "market_repricing",
        "settlement_window": "settlement_state",
        "market_window": "settlement_state",
        "uma_activity": "settlement_state",
        "market_uma": "settlement_state",
        "settlement_mispriced": "settlement_state",
        "market_closed": "settlement_state",
        "drawdown_from_entry": "position_risk",
        "drawdown_from_high": "position_risk",
        "new_position": "portfolio_change",
    }.get(rule, "market_structure")
    return {
        "signal_kind": kind,
        "subject": "position" if kind in ("position_risk", "portfolio_change") else "market",
        "source": "deterministic_rule",
        "directional": False,
    }


# ---------------------------------------------------------------- rule eval


def evaluate_position(
    pos: dict[str, Any],
    *,
    rolling_high: float | None,
    seeded: bool,
    known_asset: bool,
    settlement: dict[str, Any] | None = None,
    config: AlertConfig | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Evaluate every rule against one position; returns candidate alerts, not yet
    de-duplicated.

    `settlement` is the settlement guard's assessment. When it is None because
    metadata could not be fetched, the settlement-window rule degrades to a pure
    time warning derived from the position's own end date, and the oracle rules
    stay silent rather than inventing state.

    Verbatim quotes from resolution text are always reproduced exactly as
    written."""
    cfg = config or AlertConfig()
    now = now or datetime.now(timezone.utc)
    alerts: list[dict[str, Any]] = []

    title = pos.get("title") or "(unknown market)"
    outcome = pos.get("outcome") or "?"
    asset = str(pos.get("key") or "")
    cid = str(pos.get("conditionId") or "")
    avg = pos.get("avgPrice")
    cur = pos.get("curPrice")
    notional = _notional(pos)
    big_enough = notional >= cfg.min_notional_usd
    label = f"{title} — {outcome}"
    price_active = is_price_active(pos, now=now)

    # 1. new position (informational): only once seeded, and only for an asset
    #    seen here for the first time
    if cfg.enable_new_position and seeded and not known_asset and big_enough and price_active:
        alerts.append(
            {
                "rule": "new_position",
                "scope": f"new|{asset}",
                "bucket": 1,
                "severity": "info",
                "emoji": "🆕",
                "title": title,
                "reason": f"🆕 New position: {label} at {_fmt_price(cur)} "
                    f"(~${notional:.0f} in). Now tracking it.",
            }
        )

    # 2. drawdown from entry (live markets only)
    if price_active and big_enough and avg and avg > 0 and cur is not None and cur < avg:
        drop = (avg - cur) / avg
        if drop >= cfg.entry_drawdown:
            bucket = int(math.floor(drop / cfg.drawdown_bucket_step))
            alerts.append(
                {
                    "rule": "drawdown_from_entry",
                    "scope": f"dd_entry|{asset}",
                    "bucket": bucket,
                    "severity": "warn",
                    "emoji": "📉",
                    "title": title,
                    "reason": f"📉 {label} is down {drop*100:.0f}% from your entry: "
                        f"in at {_fmt_price(avg)}, now {_fmt_price(cur)}.",
                }
            )

    # 3. pullback from the recent high (live markets only)
    if price_active and big_enough and rolling_high and rolling_high > 0 and cur is not None and cur < rolling_high:
        off = (rolling_high - cur) / rolling_high
        if off >= cfg.high_drawdown:
            bucket = int(math.floor(off / cfg.drawdown_bucket_step))
            alerts.append(
                {
                    "rule": "drawdown_from_high",
                    "scope": f"dd_high|{asset}",
                    "bucket": bucket,
                    "severity": "warn",
                    "emoji": "🔻",
                    "title": title,
                    "reason": f"🔻 {label} pulled back {off*100:.0f}% from its recent high "
                        f"{_fmt_price(rolling_high)} to {_fmt_price(cur)}.",
                }
            )

    # 4. settlement window (T-48h / T-24h / T-2h), carrying clauses from the
    #    resolution text. With no assessment available it degrades to a pure time
    #    warning off the end date, with no quotes.
    if big_enough and not pos.get("redeemable"):
        if settlement is not None:
            hours_left = settlement.get("hours_to_end")
            window_bucket = int(settlement.get("window_bucket") or 0)
            gotchas = settlement.get("rule_gotchas") or []
        else:
            end_dt = _parse_iso(pos.get("endDate") or "")
            hours_left = (end_dt - now).total_seconds() / 3600.0 if end_dt else None
            window_bucket = 0
            gotchas = []
            if hours_left is not None and hours_left > 0:
                for i, h in enumerate(sorted(cfg.settlement_window_hours, reverse=True)):
                    if hours_left <= h:
                        window_bucket = i + 1
                if hours_left <= cfg.final_resolution_hours:
                    window_bucket = len(cfg.settlement_window_hours) + 1
        if window_bucket > 0 and hours_left is not None and hours_left > 0:
            final_bucket = len(cfg.settlement_window_hours) + 1
            near = window_bucket >= final_bucket
            val = pos.get("currentValue") or 0
            end = pos.get("endDate")
            when = (f"under {cfg.final_resolution_hours:.0f} hours" if near
                    else f"~{hours_left:.0f} hours")
            reason = (
                f"⏰ {label} enters resolution in {when} ({end}) "
                f"and you're still holding (~${val:.0f})."
            )
            if gotchas:
                # the quote reproduces the resolution text verbatim
                quoted = " ".join(f"“{g}”" for g in gotchas[:2])
                reason += f"\n📜 From the market rules — worth re-reading before it settles: {quoted}"
            alerts.append(
                {
                    "rule": "settlement_window",
                    "scope": f"resolve|{asset}",
                    "bucket": window_bucket,
                    "severity": "warn",
                    "emoji": "⏰",
                    "title": title,
                    "reason": reason,
                }
            )

    # 5. oracle propose / dispute, via the settlement guard.
    #    proposed -> bucket 1, disputed -> bucket 2.
    if settlement is not None and cid and settlement.get("phase") in ("proposed", "disputed"):
        disputed = settlement["phase"] == "disputed"
        proposed_label = settlement.get("proposed_outcome_label")
        vs = settlement.get("proposed_vs_held")
        if proposed_label is not None:
            direction = f"Proposed outcome: {proposed_label}"
            if vs == "with_you":
                direction += f" — same side as your {outcome} position."
            elif vs == "against_you":
                direction += (f" — AGAINST your {outcome} position; if it stands, "
                              f"this position expires worthless.")
            else:
                direction += "."
        else:
            # direction unreadable on-chain: say so, never invent it
            direction = "Proposed outcome details aren't available on-chain yet."
        if disputed:
            reason = (
                f"⚠️ {label}: the settlement is now DISPUTED at UMA — the first proposal "
                f"was challenged, resolution goes to a vote and may be delayed or overturned. "
                f"{direction}"
            )
        else:
            reason = (
                f"⚖️ {label}: an outcome was just proposed to the UMA oracle. "
                f"{direction} There is a challenge window before it becomes final."
            )
        alerts.append(
            {
                "rule": "uma_activity",
                "scope": f"dispute|{cid}",
                "bucket": 2 if disputed else 1,
                "severity": "alert" if disputed else "warn",
                "emoji": "⚠️" if disputed else "⚖️",
                "title": title,
                "reason": reason,
            }
        )

    # 5b. mispriced-settlement warning: the proposed outcome diverges sharply from
    #     the market price.
    if settlement is not None and cid and settlement.get("mispriced"):
        p = settlement.get("proposed_side_price")
        proposed_label = settlement.get("proposed_outcome_label") or "?"
        depth_bucket = 1 + int(
            max(0.0, cfg.mispriced_price_floor - (p or 0.0)) / cfg.mispriced_bucket_step
        )
        vs = settlement.get("proposed_vs_held")
        tail = ""
        if vs == "against_you":
            tail = f" Your {outcome} position is on the other side of this proposal."
        elif vs == "with_you":
            tail = f" Your {outcome} position is on the proposed side."
        reason = (
            f"🚨 {label}: the proposed settlement outcome ({proposed_label}) disagrees "
            f"with the market — traders only price it at {_fmt_price(p)}, far from certainty. "
            f"This is the classic mis-settlement pattern: verify the proposal against the "
            f"market rules before the challenge window closes.{tail}"
        )
        alerts.append(
            {
                "rule": "settlement_mispriced",
                "scope": f"mispriced|{cid}",
                "bucket": depth_bucket,
                "severity": "alert",
                "emoji": "🚨",
                "title": title,
                "reason": reason,
            }
        )

    return alerts


# ---------------------------------------------------------------- dedup


def should_fire(fired: dict[str, Any], alert: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether a candidate alert should actually be sent. Read-only; it does not
    modify `fired`.

    Two de-duplication semantics, selected by whether the alert carries a
    cooldown:
    - bucket worsening (the default): the same scope fires again only when its
      bucket gets worse. Used for drawdown and approaching-settlement alerts.
    - time cooldown (alert carries cooldown_sec): the same scope fires again once
      the cooldown has elapsed. Used for price jumps, which are repeat events
      with no direction of deterioration.
    """
    prev = fired.get(alert["scope"])
    if not isinstance(prev, dict):
        return True
    cooldown = alert.get("cooldown_sec")
    if cooldown is not None:
        last_at = _parse_iso(str(prev.get("at") or ""))
        if last_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        return (now - last_at).total_seconds() >= float(cooldown)
    return int(alert.get("bucket", 1)) > int(prev.get("bucket", 0))


def mark_fired(fired: dict[str, Any], alert: dict[str, Any], *, at: str) -> None:
    """Record that an alert was sent, writing the fired dict in place."""
    fired[alert["scope"]] = {"bucket": int(alert.get("bucket", 1)), "at": at, "rule": alert["rule"]}
