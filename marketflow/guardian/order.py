"""Guardian user-command BUY path (human-in-the-loop, no alpha).

Guardian automates EXITS. Entry is ALWAYS a user's explicit command: the user
picks the market and the dollar amount in Telegram, we quote the book, they
confirm, and only then do we plan a BUY through S1's fuses. This module never
originates a buy signal and never suggests what to buy — zero alpha leakage,
matching the public Guardian description.

A BUY additionally requires the tenant's arm mode to permit BUY. In v0 the arm
stays `exit_only`, so this path plans dry_run: turning it live is a deliberate,
owner-gated step (raise the tenant arm to `full` via the approved writer). The
tenant's own user-set caps bound every buy regardless.
"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import executor as gexec  # noqa: E402
from marketflow.guardian import traps as gtraps  # noqa: E402
from marketflow.guardian import wallet as gwallet  # noqa: E402


def plan_user_buy(
    tenant_id: str,
    *,
    token_id: str,
    max_spend_usd: float,
    max_price: float,
    market_slug: str | None = None,
    command_id: str,
    requested_live: bool = True,
    book: Any = None,
    caps: Any = None,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Plan a user-commanded BUY. `command_id` is the user's confirmed-order id
    (idempotency). Bounded market BUY only (max_spend + max_price); unbounded
    orders are refused by S1. Live requires GUARDIAN_LIVE_ENABLED + a tenant arm
    permitting BUY (full) — both owner-gated.

    A structural trap returns a refusal the caller shows to the user; sending the
    same command back with `override_reason` buys it anyway. The guard rail is
    the user's to decline — the fuses below it are not."""
    from marketflow.execution import orders as pmx  # type: ignore

    entry = gstore.load_registry()["tenants"].get(tenant_id)
    if caps is None:
        caps = gexec.tenant_caps(entry)

    # Screen the price they would actually pay. max_price is a ceiling: a book
    # sitting at 0.06 under a 0.30 ceiling is still a sub-dime ticket, and
    # screening the ceiling would wave it through.
    ask = gexec._live_quote(str(token_id), book=book).get("best_ask")
    trap = gtraps.screen_buy(price=(ask if ask is not None else float(max_price)),
                             tenant_id=tenant_id, entry=entry,
                             override_reason=override_reason, context="user_buy")
    if trap["blocked"]:
        gstore.audit("user_buy_trap_blocked", tenant_id=tenant_id, token_id=token_id,
                     rules=trap["blocked_by"], price=trap["verdicts"][0]["price"])
        return {"mode": "REFUSED_BY_TRAP", "will_execute_live": False, "executed": False,
                "trap": trap,
                "explain": [gtraps.explain(r) for r in trap["blocked_by"]]}
    if trap["override_reason"]:
        gstore.audit("user_buy_trap_overridden", tenant_id=tenant_id, token_id=token_id,
                     rules=trap["tripped"], reason=trap["override_reason"])

    intent = pmx.build_entry_intent(
        token_id=str(token_id),
        order_kind="market",
        max_spend_usd=float(max_spend_usd),
        max_price=float(max_price),
        market_order_type="FOK",
        taker_allowed_reason="in_play_speed_window",
        market_slug=market_slug,
        signal_id=f"{tenant_id}:buy:{command_id}",
        idempotency_key=f"{tenant_id}:buy:{command_id}",
        note="user_command_buy",
    )
    live_gate = bool(requested_live and gstore.live_enabled())
    plan = pmx.plan_order(
        intent,
        requested_live=live_gate,
        caps=caps,  # user-set, fat-finger clamped (tenant_caps)
        kill_file=gexec.tenant_kill_file(tenant_id),
        arm_state_file=gexec.tenant_arm_file(tenant_id),
        ledger_path=gexec.tenant_ledger_file(tenant_id),
        book=book,
        arm_expected_writer=gexec.GUARDIAN_ARM_WRITER,
        arm_expected_ack=gexec.GUARDIAN_LIVE_ACK,
        arm_expected_tenant=tenant_id,
    )
    plan["trap"] = trap
    gstore.audit("user_buy_planned", tenant_id=tenant_id, token_id=token_id,
                 max_spend_usd=max_spend_usd, will_execute_live=plan.get("will_execute_live"))
    return plan


def execute_user_buy(tenant_id: str, plan: dict[str, Any], intent: Any, *, master_key: bytes | None = None) -> dict[str, Any]:
    """Execute a live-cleared user BUY. Same secret-in-memory + EOA client path as
    exits; fails closed if not live-cleared."""
    from marketflow.execution import orders as pmx  # type: ignore

    if not plan.get("will_execute_live"):
        return {**plan, "executed": False, "reason": "not live-cleared; dry_run"}
    tdir = gstore.tenant_dir(tenant_id)
    secrets = gwallet.decrypt_secrets(tdir, master_key=master_key)
    client = gexec._with_hard_timeout(
        gexec.CLIENT_BUILD_TIMEOUT_SEC, "guardian live SecureClient build (buy)",
        lambda: pmx.build_secure_client(secrets, secret_dir=tdir),
    )
    try:
        record = pmx.execute_order(client, intent, plan, expected_wallet_type=gexec.EXPECTED_WALLET_TYPE,
                                   ledger_path=gexec.tenant_ledger_file(tenant_id), secret_dir=tdir)
    finally:
        try:
            client.close()
        except Exception:
            pass
    leaks = pmx.assert_no_secret_leak(record, secrets)
    if leaks:
        pmx.engage_global_halt("guardian secret leak guard tripped after live buy")
        record["secret_leak_guard"] = {"leaks": sorted(leaks), "halt_engaged": True}
    gstore.audit("user_buy_executed", tenant_id=tenant_id, executed=bool(record.get("executed")))
    return record
