"""Persistent per-tenant risk budget for automated BUY signatures.

the order module fuse caps bound one order and the Turnkey policy bounds one signature.  A
compromised or looping process can still repeat legal orders, so user-root entry
needs a second, stateful budget: daily notional, signature frequency, one-market
concentration and realized-loss stop.

The ledger is append-only and hash-chained.  Missing is a fresh zero state;
malformed/tampered is UNKNOWN and therefore blocks BUY.  An unresolved expired
reservation also blocks BUY until reconciled — silently forgetting it could
double-spend the budget after a crash.  SELL/cancel/revoke do not consult this
module and can never be blocked by it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from marketflow.guardian import store as gstore


SCHEMA = "guardian-risk-budget-v0.1"
RESERVATION_TTL_SEC = 180.0
EVENT_RESERVED = "reserved"
EVENT_COMMITTED = "committed"
EVENT_RELEASED = "released"
EVENT_REALIZED_LOSS = "realized_loss"
EVENTS = frozenset(
    {EVENT_RESERVED, EVENT_COMMITTED, EVENT_RELEASED, EVENT_REALIZED_LOSS}
)


class RiskBudgetError(RuntimeError):
    """Budget state is invalid, unknown or unavailable; BUY must stop."""


# Defaults are fractions of the declared mandate capital, the same base the executor
# uses, so a mandate of any size gets proportionate limits with no code change. The
# fractions are illustrative defaults, not a recommendation: a deployment sets its own
# per tenant under rules.risk_budget. tests/test_scale_invariance.py asserts both that
# this base agrees with the executor's and that the limits scale with it.
MANDATE_CAPITAL_USD = float(os.environ.get("MARKETFLOW_MANDATE_CAPITAL_USD") or 1_000_000.0)
DAILY_ENTRY_NOTIONAL_FRACTION = 0.20      # new exposure opened per day
MARKET_ENTRY_NOTIONAL_FRACTION = 0.05     # new exposure in any one market per day
DAILY_REALIZED_LOSS_FRACTION = 0.02       # realized loss that stops entries for the day


@dataclass(frozen=True)
class RiskLimits:
    max_daily_entry_notional_usd: float = MANDATE_CAPITAL_USD * DAILY_ENTRY_NOTIONAL_FRACTION
    max_entries_per_minute: int = 2
    max_entries_per_hour: int = 10
    max_market_entry_notional_usd: float = MANDATE_CAPITAL_USD * MARKET_ENTRY_NOTIONAL_FRACTION
    max_daily_realized_loss_usd: float = MANDATE_CAPITAL_USD * DAILY_REALIZED_LOSS_FRACTION

    def __post_init__(self) -> None:
        for name in (
            "max_daily_entry_notional_usd",
            "max_market_entry_notional_usd",
            "max_daily_realized_loss_usd",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise RiskBudgetError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        for name in ("max_entries_per_minute", "max_entries_per_hour"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) <= 0:
                raise RiskBudgetError(f"{name} must be a positive integer")
            object.__setattr__(self, name, int(value))
        if self.max_market_entry_notional_usd > self.max_daily_entry_notional_usd:
            raise RiskBudgetError(
                "max_market_entry_notional_usd cannot exceed daily notional"
            )
        if self.max_entries_per_minute > self.max_entries_per_hour:
            raise RiskBudgetError(
                "max_entries_per_minute cannot exceed max_entries_per_hour"
            )


def limits_for_entry(entry: Mapping[str, Any] | None) -> RiskLimits:
    """Resolve explicit user settings, with conservative defaults.

    Values live under ``rules.risk_budget`` so they do not collide with the order module's
    existing per-order and total deployment caps.
    """
    rules = (entry or {}).get("rules")
    risk = rules.get("risk_budget") if isinstance(rules, Mapping) else None
    risk = risk if isinstance(risk, Mapping) else {}
    return RiskLimits(
        max_daily_entry_notional_usd=risk.get(
            "max_daily_entry_notional_usd", RiskLimits.max_daily_entry_notional_usd),
        max_entries_per_minute=risk.get("max_entries_per_minute", 2),
        max_entries_per_hour=risk.get("max_entries_per_hour", 10),
        max_market_entry_notional_usd=risk.get(
            "max_market_entry_notional_usd", RiskLimits.max_market_entry_notional_usd),
        max_daily_realized_loss_usd=risk.get(
            "max_daily_realized_loss_usd", RiskLimits.max_daily_realized_loss_usd),
    )


def ledger_path(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(str(tenant_id)), "risk_budget.jsonl")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _finite_positive(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskBudgetError(f"{field} must be finite and positive") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise RiskBudgetError(f"{field} must be finite and positive")
    return round(parsed, 6)


def _timestamp(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskBudgetError(f"{field} must be a finite timestamp") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise RiskBudgetError(f"{field} must be a finite timestamp")
    return parsed


def _read_locked(handle) -> list[dict[str, Any]]:
    handle.seek(0)
    rows: list[dict[str, Any]] = []
    previous_hash = ""
    expected_sequence = 1
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RiskBudgetError(f"risk ledger malformed at line {line_number}") from exc
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            raise RiskBudgetError(f"risk ledger schema invalid at line {line_number}")
        if row.get("event") not in EVENTS:
            raise RiskBudgetError(f"risk ledger event invalid at line {line_number}")
        if row.get("sequence") != expected_sequence:
            raise RiskBudgetError(f"risk ledger sequence break at line {line_number}")
        if row.get("previous_hash") != previous_hash:
            raise RiskBudgetError(f"risk ledger chain break at line {line_number}")
        claimed = str(row.get("event_hash") or "")
        body = dict(row)
        body.pop("event_hash", None)
        if not claimed or claimed != _hash(body):
            raise RiskBudgetError(f"risk ledger tamper detected at line {line_number}")
        _timestamp(row.get("ts"), "ts")
        rows.append(row)
        previous_hash = claimed
        expected_sequence += 1
    _validate_history(rows)
    return rows


def load_events(tenant_id: str, *, path: str | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path(tenant_id)
    try:
        with open(target, encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_locked(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RiskBudgetError("risk ledger unavailable") from exc


def _reservation_states(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = row["event"]
        if event == EVENT_REALIZED_LOSS:
            continue
        reservation_id = str(row.get("reservation_id") or "")
        if not reservation_id:
            raise RiskBudgetError("risk ledger reservation id missing")
        if event == EVENT_RESERVED:
            if reservation_id in states:
                raise RiskBudgetError("duplicate risk reservation")
            states[reservation_id] = {"status": EVENT_RESERVED, **dict(row)}
            continue
        state = states.get(reservation_id)
        if state is None or state.get("status") != EVENT_RESERVED:
            raise RiskBudgetError("risk reservation transition invalid")
        if event == EVENT_COMMITTED:
            committed = _finite_positive(row.get("filled_notional_usd"), "filled_notional_usd")
            if committed > float(state["notional_usd"]) + 1e-6:
                raise RiskBudgetError("committed notional exceeds reserved notional")
            state.update({"status": EVENT_COMMITTED, "filled_notional_usd": committed,
                          "final_ts": row["ts"]})
        elif event == EVENT_RELEASED:
            state.update({"status": EVENT_RELEASED, "release_reason": row.get("reason"),
                          "final_ts": row["ts"]})
    return states


def _validate_history(rows: list[Mapping[str, Any]]) -> None:
    _reservation_states(rows)
    seen_loss_ids: set[str] = set()
    for row in rows:
        if row["event"] != EVENT_REALIZED_LOSS:
            continue
        event_id = str(row.get("loss_id") or "")
        if not event_id or event_id in seen_loss_ids:
            raise RiskBudgetError("realized-loss id missing or duplicated")
        seen_loss_ids.add(event_id)
        _finite_positive(row.get("loss_usd"), "loss_usd")


def _append_under_lock(handle, rows: list[dict[str, Any]], event: Mapping[str, Any]) -> dict[str, Any]:
    row = {"schema": SCHEMA, **dict(event)}
    row["sequence"] = len(rows) + 1
    row["previous_hash"] = str(rows[-1]["event_hash"]) if rows else ""
    row["event_hash"] = _hash(row)
    handle.seek(0, os.SEEK_END)
    handle.write(_canonical(row) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    rows.append(row)
    _validate_history(rows)
    return row


def _day_start(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp()


def _usage(rows: list[dict[str, Any]], *, now: float) -> dict[str, Any]:
    states = _reservation_states(rows)
    stale_unknown: list[str] = []
    counted: list[dict[str, Any]] = []
    for reservation_id, state in states.items():
        status = state["status"]
        if status == EVENT_RELEASED:
            continue
        if status == EVENT_RESERVED and float(state["expires_at"]) < now:
            stale_unknown.append(reservation_id)
            continue
        counted.append(state)
    day_start = _day_start(now)
    today = [r for r in counted if float(r["ts"]) >= day_start]
    losses = [
        r for r in rows
        if r["event"] == EVENT_REALIZED_LOSS and float(r["ts"]) >= day_start
    ]

    def notional(state: Mapping[str, Any]) -> float:
        if state["status"] == EVENT_COMMITTED:
            return float(state["filled_notional_usd"])
        return float(state["notional_usd"])

    by_market: dict[str, float] = {}
    for state in today:
        market = str(state["market_id"])
        by_market[market] = round(by_market.get(market, 0.0) + notional(state), 6)
    return {
        "stale_unknown": stale_unknown,
        "daily_notional_usd": round(sum(notional(r) for r in today), 6),
        "market_notional_usd": by_market,
        "entries_last_minute": sum(1 for r in counted if float(r["ts"]) >= now - 60),
        "entries_last_hour": sum(1 for r in counted if float(r["ts"]) >= now - 3600),
        "daily_realized_loss_usd": round(sum(float(r["loss_usd"]) for r in losses), 6),
        "reservations": states,
    }


def reserve_entry(
    tenant_id: str,
    *,
    idempotency_key: str,
    market_id: str,
    notional_usd: float,
    limits: RiskLimits,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Atomically reserve BUY budget before a plan can reach a signer."""
    now_ts = float(time.time() if now is None else now)
    _timestamp(now_ts, "now")
    key = str(idempotency_key or "").strip()
    market = str(market_id or "").strip()
    if not key or not market:
        raise RiskBudgetError("entry budget requires idempotency_key and market_id")
    notional = _finite_positive(notional_usd, "notional_usd")
    target = path or ledger_path(tenant_id)
    os.makedirs(os.path.dirname(os.path.abspath(target)), mode=0o700, exist_ok=True)
    try:
        with open(target, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                rows = _read_locked(handle)
                states = _reservation_states(rows)
                for reservation_id, state in states.items():
                    if state.get("idempotency_key") != key:
                        continue
                    same = (
                        state.get("market_id") == market
                        and abs(float(state.get("notional_usd")) - notional) <= 1e-6
                    )
                    if not same:
                        raise RiskBudgetError("idempotency key conflicts with prior reservation")
                    if state["status"] == EVENT_RELEASED:
                        raise RiskBudgetError("released reservation cannot be reused")
                    if state["status"] == EVENT_COMMITTED:
                        raise RiskBudgetError("entry already committed")
                    return {
                        "allowed": True,
                        "idempotent": True,
                        "reservation_id": reservation_id,
                        "status": state["status"],
                    }
                usage = _usage(rows, now=now_ts)
                if usage["stale_unknown"]:
                    raise RiskBudgetError("unreconciled expired reservation blocks BUY")
                if usage["daily_realized_loss_usd"] >= limits.max_daily_realized_loss_usd:
                    raise RiskBudgetError("daily realized-loss budget exhausted")
                if usage["entries_last_minute"] >= limits.max_entries_per_minute:
                    raise RiskBudgetError("per-minute entry frequency exhausted")
                if usage["entries_last_hour"] >= limits.max_entries_per_hour:
                    raise RiskBudgetError("per-hour entry frequency exhausted")
                if usage["daily_notional_usd"] + notional > limits.max_daily_entry_notional_usd + 1e-6:
                    raise RiskBudgetError("daily entry notional budget exhausted")
                market_used = usage["market_notional_usd"].get(market, 0.0)
                if market_used + notional > limits.max_market_entry_notional_usd + 1e-6:
                    raise RiskBudgetError("single-market concentration budget exhausted")
                reservation_id = "rb_" + uuid.uuid4().hex
                row = _append_under_lock(
                    handle,
                    rows,
                    {
                        "event": EVENT_RESERVED,
                        "ts": now_ts,
                        "tenant_id": str(tenant_id),
                        "reservation_id": reservation_id,
                        "idempotency_key": key,
                        "market_id": market,
                        "notional_usd": notional,
                        "expires_at": now_ts + RESERVATION_TTL_SEC,
                    },
                )
                return {
                    "allowed": True,
                    "idempotent": False,
                    "reservation_id": reservation_id,
                    "expires_at": row["expires_at"],
                }
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except RiskBudgetError:
        raise
    except OSError as exc:
        raise RiskBudgetError("risk budget unavailable; BUY refused") from exc


def validate_reservation(
    tenant_id: str,
    reservation_id: str,
    *,
    market_id: str,
    notional_usd: float,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Re-check the exact reservation immediately before signing."""
    now_ts = float(time.time() if now is None else now)
    rows = load_events(tenant_id, path=path)
    usage = _usage(rows, now=now_ts)
    if usage["stale_unknown"]:
        raise RiskBudgetError("unreconciled expired reservation blocks BUY")
    state = usage["reservations"].get(str(reservation_id))
    if not state or state["status"] != EVENT_RESERVED:
        raise RiskBudgetError("entry reservation is missing or no longer active")
    if float(state["expires_at"]) < now_ts:
        raise RiskBudgetError("entry reservation expired before signing")
    if state["market_id"] != str(market_id):
        raise RiskBudgetError("entry reservation market mismatch")
    if abs(float(state["notional_usd"]) - _finite_positive(notional_usd, "notional_usd")) > 1e-6:
        raise RiskBudgetError("entry reservation notional mismatch")
    return {"allowed": True, "reservation_id": reservation_id, "expires_at": state["expires_at"]}


def _finalize_reservation(
    tenant_id: str,
    reservation_id: str,
    *,
    event: str,
    now: float | None,
    path: str | None,
    filled_notional_usd: float | None = None,
    reason: str = "",
) -> dict[str, Any]:
    now_ts = float(time.time() if now is None else now)
    target = path or ledger_path(tenant_id)
    try:
        with open(target, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                rows = _read_locked(handle)
                state = _reservation_states(rows).get(str(reservation_id))
                if not state:
                    raise RiskBudgetError("risk reservation not found")
                if state["status"] == event:
                    return {"ok": True, "idempotent": True, "reservation_id": reservation_id}
                if state["status"] != EVENT_RESERVED:
                    raise RiskBudgetError("risk reservation already finalized differently")
                payload: dict[str, Any] = {
                    "event": event,
                    "ts": now_ts,
                    "tenant_id": str(tenant_id),
                    "reservation_id": str(reservation_id),
                }
                if event == EVENT_COMMITTED:
                    payload["filled_notional_usd"] = _finite_positive(
                        filled_notional_usd, "filled_notional_usd"
                    )
                else:
                    payload["reason"] = str(reason or "not_submitted")[:200]
                _append_under_lock(handle, rows, payload)
                return {"ok": True, "idempotent": False, "reservation_id": reservation_id}
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except RiskBudgetError:
        raise
    except OSError as exc:
        raise RiskBudgetError("risk budget unavailable") from exc


def commit_entry(
    tenant_id: str,
    reservation_id: str,
    *,
    filled_notional_usd: float,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    return _finalize_reservation(
        tenant_id,
        reservation_id,
        event=EVENT_COMMITTED,
        now=now,
        path=path,
        filled_notional_usd=filled_notional_usd,
    )


def release_entry(
    tenant_id: str,
    reservation_id: str,
    *,
    reason: str,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    return _finalize_reservation(
        tenant_id,
        reservation_id,
        event=EVENT_RELEASED,
        now=now,
        path=path,
        reason=reason,
    )


def record_realized_loss(
    tenant_id: str,
    *,
    loss_id: str,
    loss_usd: float,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Append an observed realized loss. Duplicate loss ids are idempotent."""
    now_ts = float(time.time() if now is None else now)
    target = path or ledger_path(tenant_id)
    key = str(loss_id or "").strip()
    if not key:
        raise RiskBudgetError("loss_id is required")
    loss = _finite_positive(loss_usd, "loss_usd")
    os.makedirs(os.path.dirname(os.path.abspath(target)), mode=0o700, exist_ok=True)
    try:
        with open(target, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                rows = _read_locked(handle)
                for row in rows:
                    if row["event"] == EVENT_REALIZED_LOSS and row.get("loss_id") == key:
                        if abs(float(row["loss_usd"]) - loss) > 1e-6:
                            raise RiskBudgetError("loss_id conflicts with prior loss")
                        return {"ok": True, "idempotent": True, "loss_id": key}
                _append_under_lock(
                    handle,
                    rows,
                    {
                        "event": EVENT_REALIZED_LOSS,
                        "ts": now_ts,
                        "tenant_id": str(tenant_id),
                        "loss_id": key,
                        "loss_usd": loss,
                    },
                )
                return {"ok": True, "idempotent": False, "loss_id": key}
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except RiskBudgetError:
        raise
    except OSError as exc:
        raise RiskBudgetError("risk budget unavailable") from exc


def buy_gate(
    tenant_id: str,
    *,
    limits: RiskLimits,
    now: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Read-only status: any unknown/corrupt state blocks BUY."""
    try:
        now_ts = float(time.time() if now is None else now)
        usage = _usage(load_events(tenant_id, path=path), now=now_ts)
        if usage["stale_unknown"]:
            raise RiskBudgetError("unreconciled expired reservation")
        reasons = []
        if usage["daily_realized_loss_usd"] >= limits.max_daily_realized_loss_usd:
            reasons.append("daily_realized_loss_exhausted")
        return {
            "block_new_entries": bool(reasons),
            "allow_exit": True,
            "allow_cancel": True,
            "allow_revoke": True,
            "reasons": reasons,
            "usage": {k: v for k, v in usage.items() if k != "reservations"},
        }
    except Exception as exc:
        return {
            "block_new_entries": True,
            "allow_exit": True,
            "allow_cancel": True,
            "allow_revoke": True,
            "reasons": [f"risk_budget_unavailable:{type(exc).__name__}"],
        }


def selftest() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    path = os.path.join(gstore.GUARDIAN_ROOT, "risk_budget_selftest.jsonl")
    limits = RiskLimits(
        max_daily_entry_notional_usd=100,
        max_entries_per_minute=2,
        max_entries_per_hour=3,
        max_market_entry_notional_usd=60,
        max_daily_realized_loss_usd=20,
    )
    t0 = datetime(2026, 8, 9, tzinfo=timezone.utc).timestamp()
    one = reserve_entry(
        "tgRISK1", idempotency_key="k1", market_id="m1", notional_usd=40,
        limits=limits, now=t0, path=path,
    )
    checks["first_reservation_allowed"] = one["allowed"] and not one["idempotent"]
    again = reserve_entry(
        "tgRISK1", idempotency_key="k1", market_id="m1", notional_usd=40,
        limits=limits, now=t0 + 1, path=path,
    )
    checks["reservation_idempotent"] = again["reservation_id"] == one["reservation_id"]
    validate_reservation(
        "tgRISK1", one["reservation_id"], market_id="m1", notional_usd=40,
        now=t0 + 2, path=path,
    )
    checks["reservation_rechecked_before_sign"] = True
    commit_entry(
        "tgRISK1", one["reservation_id"], filled_notional_usd=35,
        now=t0 + 3, path=path,
    )
    checks["commit_survives_reload"] = (
        _usage(load_events("tgRISK1", path=path), now=t0 + 4)["daily_notional_usd"] == 35
    )
    two = reserve_entry(
        "tgRISK1", idempotency_key="k2", market_id="m2", notional_usd=24,
        limits=limits, now=t0 + 61, path=path,
    )
    release_entry(
        "tgRISK1", two["reservation_id"], reason="not_submitted", now=t0 + 62, path=path,
    )
    checks["released_reservation_frees_budget"] = (
        _usage(load_events("tgRISK1", path=path), now=t0 + 63)["daily_notional_usd"] == 35
    )

    def denied(**kwargs: Any) -> bool:
        try:
            reserve_entry("tgRISK1", limits=limits, path=path, **kwargs)
        except RiskBudgetError:
            return True
        return False

    checks["market_concentration_blocks"] = denied(
        idempotency_key="k3", market_id="m1", notional_usd=30, now=t0 + 64
    )
    checks["daily_notional_blocks"] = denied(
        idempotency_key="k4", market_id="m3", notional_usd=70, now=t0 + 64
    )
    record_realized_loss(
        "tgRISK1", loss_id="loss1", loss_usd=20, now=t0 + 65, path=path,
    )
    checks["loss_budget_blocks"] = denied(
        idempotency_key="k5", market_id="m3", notional_usd=1, now=t0 + 66
    )
    gate = buy_gate("tgRISK1", limits=limits, now=t0 + 67, path=path)
    checks["loss_gate_never_blocks_exit"] = (
        gate["block_new_entries"] and gate["allow_exit"] and gate["allow_cancel"]
        and gate["allow_revoke"]
    )

    stale_path = os.path.join(gstore.GUARDIAN_ROOT, "risk_budget_stale_selftest.jsonl")
    stale = reserve_entry(
        "tgRISK2", idempotency_key="stale", market_id="m1", notional_usd=1,
        limits=limits, now=t0, path=stale_path,
    )
    checks["unresolved_expired_blocks"] = denied_for_path(
        stale_path,
        tenant_id="tgRISK2",
        idempotency_key="next",
        market_id="m2",
        notional_usd=1,
        limits=limits,
        now=t0 + RESERVATION_TTL_SEC + 1,
    )
    release_entry(
        "tgRISK2", stale["reservation_id"], reason="reconciled_not_submitted",
        now=t0 + RESERVATION_TTL_SEC + 2, path=stale_path,
    )
    checks["explicit_reconcile_reopens"] = reserve_entry(
        "tgRISK2", idempotency_key="next", market_id="m2", notional_usd=1,
        limits=limits, now=t0 + RESERVATION_TTL_SEC + 3, path=stale_path,
    )["allowed"]
    with open(stale_path, "r+", encoding="utf-8") as handle:
        raw = handle.read()
        handle.seek(0)
        handle.write(raw.replace('"notional_usd":1.0', '"notional_usd":9.0', 1))
        handle.truncate()
    corrupted = buy_gate("tgRISK2", limits=limits, now=t0 + 999, path=stale_path)
    checks["tamper_blocks_buy_not_exit"] = (
        corrupted["block_new_entries"] and corrupted["allow_exit"]
    )
    return checks


def denied_for_path(path: str, **kwargs: Any) -> bool:
    try:
        reserve_entry(path=path, **kwargs)
    except RiskBudgetError:
        return True
    return False


if __name__ == "__main__":
    print("run risk_budget.selftest through marketflow/guardian/selftest.py (temp runtime only)")
    raise SystemExit(2)
