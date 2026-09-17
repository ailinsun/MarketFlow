"""Guardian resident service: automation over hosted tenants.

Each tick, for every ready/paused guardian tenant:
  1. read its funder wallet's PUBLIC positions (zero-credential data-api, same
     source the alert tier uses),
  2. run the shared multitenant brain (`evaluate_position`: user stop/take +
     graduated de-risk + settlement guard),
  3. for a SELL-side decision, plan an EXIT through the guardian executor
     (welded caps + guardian arm + EOA wallet); execute only if live-cleared,
  4. for tenants armed `entry_flb`, plan ENTRIES from allow-listed signals under
     the fleet fan-out guards,
  5. record decisions/alerts to the tenant namespace and audit trail.

Entry is bounded by four independent things, any one of which stops it: the
fleet gate `GUARDIAN_ENTRY_ENABLED`, the tenant's own arm mode, the tenant's caps
and risk profile, and the fleet guards in `fanout.py`. Exits are never gated by
the entry side — during any incident the safe direction stays open.

Withdrawal remains human-approved (store.request_withdrawal); this layer never
moves funds between accounts.

Isolation: one tenant's failure is caught and written to its own record; it never
stops another tenant. the owner's own live stack is never read or written here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import executor as gexec  # noqa: E402
from marketflow.guardian import fanout as gfan  # noqa: E402  (fleet-level entry guards; can only refuse/shrink)
from marketflow.guardian import traps as gtraps  # noqa: E402  (structural-trap rules; refuse + advise only)

# The multitenant brain + public data plane are reused wholesale (decision logic,
# settlement guard, position mapping). Guardian only adds the hosted-exec wiring.
from marketflow.execution import multitenant as mt  # noqa: E402
from marketflow.execution import orders as pmx  # noqa: E402

# Entitlement provider (who is allowed to OPEN new positions) is deliberately not
# shipped: billing, plans and invoicing are the deployment's own business logic.
# Point MARKETFLOW_ENTITLEMENT_MODULE at an importable module exposing
#     allows_entry(chat_id: str) -> bool
# With nothing configured the gate stays CLOSED for entry (exits are unaffected).
def _entitlement_module():
    name = os.environ.get("MARKETFLOW_ENTITLEMENT_MODULE", "").strip()
    if not name:
        return None
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None

ACTIVE_STATUSES = ("ready", "funded")  # paused tenants are skipped for actions


def subscription_ok(chat_id: str) -> bool:
    """Whether this tenant may open NEW positions.

    Entitlement uncertainty fails closed for BUY only. This function is called
    only from the entry path; exits are planned and executed before it and
    therefore remain available through an entitlement-provider outage or a
    past-due state. With no provider configured, entry is refused for everyone —
    that is the safe default, not a bug.
    """
    mod = _entitlement_module()
    if mod is None:
        return False
    try:
        return bool(mod.allows_entry(chat_id))
    except Exception:
        return False


def current_plan(chat_id: str) -> str | None:
    """Best-effort plan lookup for per-tenant ceilings (Prime). None on failure."""
    if _billing_mod is None:
        return None
    try:
        return _billing_mod.plan_of(chat_id)
    except Exception:
        return None


def _tenant_config(entry: dict[str, Any]) -> mt.TenantConfig:
    """Map a guardian registry entry onto a multitenant TenantConfig so the shared
    brain can decide. Rules come from the tenant's saved prefs; caps stay welded."""
    rules = entry.get("rules") if isinstance(entry.get("rules"), dict) else {}
    return mt.TenantConfig(
        tenant_id=entry["tenant_id"],
        mode="alert_only",  # decisions only here; execution is the guardian executor
        public_wallet=entry.get("funder_address"),
        caps=gexec.tenant_caps(entry),  # user-set risk prefs, fat-finger clamped
        rules=mt.TenantRules.from_dict(rules),
        alert_channel="none",
        root=gstore.tenant_dir(entry["tenant_id"]),
    )


ENTRY_SIGNAL_QUEUE = runtime_path("guardian", "polymarket_agent_intents.jsonl")


def read_entry_signals(*, queue_path: str = ENTRY_SIGNAL_QUEUE, max_age_sec: float = 1800.0,
                       now_ts: float | None = None) -> list[dict[str, Any]]:
    """Allow-listed, unexpired entry signals from the shared emitter queue.

    Read-only: guardian consumes the same signals the owner stack emits, and never
    writes to this queue. Stale signals are dropped here rather than downstream —
    a signal older than its TTL describes a market that has moved on."""
    now = float(now_ts if now_ts is not None else time.time())
    out: list[dict[str, Any]] = []
    try:
        with open(queue_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("source") or "") not in gexec.ENTRY_SOURCE_ALLOWLIST:
                    continue
                created = str(row.get("created_at") or "")
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if now - dt.timestamp() > max_age_sec:
                            continue
                    except ValueError:
                        continue
                out.append(row)
    except OSError:
        return []
    return out


def _opened_today(tid: str, *, now_ts: float | None = None) -> int:
    """Positions this tenant opened today (UTC), from its own execution ledger."""
    day = datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()),
                                 tz=timezone.utc).strftime("%Y-%m-%d")
    n = 0
    try:
        with open(gexec.tenant_ledger_file(tid), encoding="utf-8") as fh:
            for line in fh:
                if '"BUY"' not in line and '"ENTER"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("generated_at") or "").startswith(day):
                    n += 1
    except OSError:
        return 0
    return n


def run_entries(entry: dict[str, Any], *, signals: list[dict[str, Any]],
                budget: Any, fleet_halt: dict[str, Any]) -> list[dict[str, Any]]:
    """Plan (and if live-cleared, execute) entries for one tenant.

    Returns one row per signal considered, so a tick is auditable: why a tenant
    did nothing is as important as what it did."""
    tid = entry["tenant_id"]
    rows: list[dict[str, Any]] = []
    if fleet_halt.get("halted"):
        return [{"skipped": "fleet_entry_halted", "detail": fleet_halt.get("reason")}]
    if not gstore.entry_enabled():
        return [{"skipped": "entry_gate_closed"}]
    if not subscription_ok(str(entry.get("chat_id"))):
        return [{"skipped": "subscription_lapsed"}]
    caps = gexec.tenant_caps(entry)
    available = gexec.tenant_collateral_usd(entry.get("funder_address"))
    opened = _opened_today(tid)
    for sig in signals:
        plan = gexec.plan_entry_for_intent(
            tid, sig, entry=entry, caps=caps,
            available_usd=available, opened_today=opened,
        )
        if plan is None:
            continue
        g = plan.get("guardian") or {}
        notional = float(g.get("estimated_notional_usd") or 0.0)
        market_id = str(sig.get("market_id") or "")
        # Fleet share of this book. Unknown depth => no depth limit (per-tenant
        # caps and the exchange still bound it); known depth => shrink to fit.
        depth = gfan.book_depth_usd({}, levels=sig.get("_ask_levels"))
        headroom = budget.headroom(market_id, depth)
        if headroom is not None and notional > headroom:
            rows.append({"market_id": market_id, "skipped": "fleet_book_share_exhausted",
                         "notional_usd": notional, "headroom_usd": headroom})
            continue
        row: dict[str, Any] = {
            "market_id": market_id,
            "market_slug": sig.get("market_slug"),
            "will_execute_live": plan.get("will_execute_live"),
            "mode": plan.get("mode"),
            "notional_usd": notional,
            "risk_profile": g.get("risk_profile"),
            "ask_price": g.get("ask_price"),
            "ask_price_source": g.get("ask_price_source"),
        }
        if plan.get("will_execute_live"):
            if entry.get("authority_mode") == "turnkey_user_root":
                from marketflow.guardian import risk_budget as grisk

                try:
                    reservation = grisk.reserve_entry(
                        tid,
                        idempotency_key=str(plan.get("idempotency_key") or ""),
                        market_id=market_id,
                        notional_usd=notional,
                        limits=grisk.limits_for_entry(entry),
                    )
                except grisk.RiskBudgetError as exc:
                    row["executed"] = False
                    row["skipped"] = "risk_budget_refused"
                    row["detail"] = str(exc)
                    rows.append(row)
                    gstore.audit(
                        "entry_risk_budget_refused", tenant_id=tid,
                        market_id=market_id, reason=str(exc),
                    )
                    continue
                plan.setdefault("guardian", {})["risk_reservation_id"] = reservation["reservation_id"]
            record = gexec.execute_entry_plan(tid, plan)
            row["executed"] = bool(record.get("executed"))
            if row["executed"]:
                opened += 1
                budget.commit(market_id, notional)
                if available is not None:
                    available = max(0.0, available - notional)
        rows.append(row)
    return rows


def run_tenant(entry: dict[str, Any], *, signals: list[dict[str, Any]] | None = None,
               budget: Any = None, fleet_halt: dict[str, Any] | None = None) -> dict[str, Any]:
    tid = entry["tenant_id"]
    result: dict[str, Any] = {
        "generated_at": gstore.iso_now(),
        "tenant_id": tid,
        "status": entry.get("status"),
        "live_enabled_globally": gstore.live_enabled(),
        "entry_enabled_globally": gstore.entry_enabled(),
        "decisions": [],
        "executions": [],
        "entries": [],
        "error": None,
    }
    try:
        cfg = _tenant_config(entry)
        if not cfg.public_wallet:
            result["error"] = "no funder address; onboarding incomplete"
            return result
        positions = mt.public_position_source(cfg)
        result["zombie"] = gtraps.zombie_check(tid, positions)
        # Refresh before any BUY is planned below: the night rule reads whatever
        # zone is on disk at plan time, and a stale one is a wrong local hour.
        try:
            result["tz_profile"] = gtraps.refresh_tz_profile(
                tid, entry.get("funder_address"), entry=entry)
        except Exception as exc:  # a zone lookup must never cost an exit
            result["tz_profile"] = {"error": type(exc).__name__}
        for pos in positions:
            decision = mt.evaluate_position(cfg, pos)
            result["decisions"].append(decision)
            mt.append_jsonl(cfg.ledger_file, decision)
            plan = gexec.plan_exit_for_decision(tid, decision, pos, caps=cfg.caps)
            if plan is None:
                continue
            exec_row: dict[str, Any] = {
                "decision": decision.get("decision"),
                "will_execute_live": plan.get("will_execute_live"),
                "mode": plan.get("mode"),
            }
            if plan.get("will_execute_live"):
                # Only reached once GUARDIAN_LIVE_ENABLED + a valid guardian arm
                # exist (owner-approved). Rebuild the exact intent the plan gated.
                intent = _rebuild_exit_intent(tid, decision, pos)
                record = gexec.execute_exit_plan(tid, plan, intent)
                exec_row["executed"] = bool(record.get("executed"))
            result["executions"].append(exec_row)
        # Entry runs AFTER exits, always. Exits reduce risk and free collateral;
        # an entry that jumped the queue could spend the very balance an exit was
        # about to need. Entry failures are isolated so they cannot break exits.
        if signals:
            try:
                result["entries"] = run_entries(
                    entry, signals=signals, budget=budget or gfan.TickBudget(),
                    fleet_halt=fleet_halt or {"halted": False},
                )
            except Exception as exc:
                result["entries"] = [{"error": {"type": type(exc).__name__, "message": str(exc)}}]
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "traceback": traceback.format_exc(limit=4)}
    mt.write_json(os.path.join(gstore.tenant_dir(tid), "latest.json"), result)
    return result


def _rebuild_exit_intent(tid: str, decision: dict[str, Any], pos: dict[str, Any]) -> Any:
    """Rebuild the OrderIntent identical to what plan_exit_for_decision gated, so
    execute_order signs exactly the planned order (same idempotency key)."""
    held = pmx.to_float(pos.get("held_shares")) or 0.0
    frac = 0.5 if decision.get("decision") == "TRIM_SELL_SIGNAL" else 1.0
    min_price = pmx.to_float(pos.get("current_sell_price") or pos.get("break_even_probability")) or pmx.MIN_LIMIT_PRICE
    token_id = decision.get("token_id") or pos.get("token_id")
    return pmx.build_exit_intent(
        token_id=str(token_id), held_shares=held, order_kind="market",
        size=round(held * frac, 6),
        min_price=max(pmx.MIN_LIMIT_PRICE, round(min_price * 0.98, 4)),
        market_slug=decision.get("market_slug"),
        signal_id=f"{tid}:{decision.get('rule_fired')}:{token_id}",
        idempotency_key=f"{tid}:{decision.get('rule_fired')}:{token_id}",
        note=decision.get("reason"),
    )


def run_all(*, tick_seed: Any = None) -> dict[str, Any]:
    doc = gstore.load_registry()
    active = {tid: e for tid, e in (doc.get("tenants") or {}).items()
              if e.get("status") in ACTIVE_STATUSES}
    signals = read_entry_signals() if gstore.entry_enabled() else []
    fleet_halt = gfan.entry_halted(gfan.load_state())
    budget = gfan.TickBudget()
    # Rotate who is served first each tick. With a fixed order the same tenants
    # would always get the better fill on a thin book — a systematic transfer
    # between customers, which a hosted product must not have.
    seed = tick_seed if tick_seed is not None else gstore.iso_now()
    order = gfan.tenant_order(sorted(active), tick_seed=seed)
    runs = []
    for tid in order:
        entry = active[tid]
        # refresh plan (Prime tier gets higher fat-finger ceilings in tenant_caps)
        plan = current_plan(str(entry.get("chat_id")))
        if plan is not None and plan != entry.get("plan"):
            entry["plan"] = plan
            gstore.upsert_tenant(entry)
        if not subscription_ok(str(entry.get("chat_id"))):
            gstore.audit("subscription_lapsed_skip_buy", tenant_id=tid)
            # still run exits (protect open positions); run_entries refuses BUY.
        runs.append(run_tenant(entry, signals=signals, budget=budget, fleet_halt=fleet_halt))
    return {
        "generated_at": gstore.iso_now(),
        "tenant_count": len(runs),
        "live_enabled_globally": gstore.live_enabled(),
        "entry_enabled_globally": gstore.entry_enabled(),
        "entry_signals": len(signals),
        "fleet_entry_halt": fleet_halt,
        "tenant_order": order,
        "runs": runs,
    }


def watch(interval_sec: float = 60.0, max_ticks: int | None = None) -> int:
    ticks = 0
    while True:
        out = run_all()
        print(json.dumps({
            "generated_at": out["generated_at"],
            "tenants": out["tenant_count"],
            "live": out["live_enabled_globally"],
            "errors": sum(1 for r in out["runs"] if r.get("error")),
        }, ensure_ascii=False), flush=True)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return 0
        time.sleep(max(5.0, interval_sec))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Guardian hosted exit-only execution service.")
    ap.add_argument("--run", action="store_true", help="one tick over all active tenants")
    ap.add_argument("--watch", action="store_true", help="resident loop")
    ap.add_argument("--once", action="store_true", help="one tick then exit")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args(argv)
    if args.watch:
        return watch(args.interval)
    if args.once:
        return watch(args.interval, max_ticks=1)
    out = run_all()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
