"""Guardian per-tenant execution adapter (exits + allow-listed automated entry).

Turns a multitenant DECISION (exit) or an allow-listed SIGNAL (entry) into a
Polymarket order through S1's canonical fuse gate, with hosted-tenant hardening
layered on top of the single-tenant stack — never widening a single fuse:

  * caps          : USER-SET per tenant (their own risk preference), clamped only
                    by fat-finger ceilings; conservative starter defaults when
                    unset. NEVER owner_fuse_caps (the owner's own stack path).
  * arm           : guardian writer/ack + tenant-bound arm file,
                    plus a global GUARDIAN_LIVE_ENABLED kill that forces dry_run
                    for ALL tenants.
  * wallet        : expected_wallet_type = DEPOSIT_WALLET (hosted wallets are
                    Polymarket Deposit Wallet proxies, same as the owner stack).
  * secrets       : decrypted from the tenant's Fernet blob ONLY when a plan is
                    live-cleared; handed straight to build_secure_client(dict);
                    never written to disk in plaintext.
  * side          : SELL is always available. BUY (260725, spec
                    260725_guardian-entry-capability-ruling) requires ALL of:
                    arm mode `entry_flb`, the fleet gate GUARDIAN_ENTRY_ENABLED,
                    a signal whose source is in ENTRY_SOURCE_ALLOWLIST, a price
                    inside the tenant profile's evidence-backed band, and room
                    under caps / balance / daily position count. Exits are never
                    gated by any of the entry conditions — during an incident the
                    risk-reducing direction must stay open.

Every path is dry_run unless (GUARDIAN_LIVE_ENABLED present) AND (tenant arm file
valid for guardian expectations) AND (S1's own fuses pass). Any doubt → dry_run.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import wallet as gwallet  # noqa: E402
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import traps as gtraps  # noqa: E402  (structural-trap rules; can only refuse)

# Guardian arm identity. These are the writer/ack a guardian
# tenant arm file must carry; they are distinct from the bridge single-tenant
# path so neither can ever arm the other.
GUARDIAN_ARM_WRITER = "guardian_service"
GUARDIAN_LIVE_ACK = "GUARDIAN_TENANT_APPROVES_AUTO_EXIT"

# Hosted wallets are Polymarket Deposit Wallets: SecureClient.create derives (and
# on first use gaslessly deploys) a Deposit Wallet proxy for the generated EOA;
# the client ACTS ON that proxy, so detect_wallet reports DEPOSIT_WALLET — same
# funder model as the owner's own stack. (An "EOA" assertion here would fail every
# live exit at the pre-sign wallet check.)
EXPECTED_WALLET_TYPE = "DEPOSIT_WALLET"
CLIENT_BUILD_TIMEOUT_SEC = 25.0

# --- Mandate scale -----------------------------------------------------------
# A mandate is one delegated book: one isolated wallet, one set of caps, one
# audit trail. Caps are the mandate holder's own risk policy; the ceilings below
# are fat-finger guards (they block a stray extra zero) expressed as fractions of
# the mandate's declared capital, so a $2M mandate and a $2B mandate use the same
# code path. Set per deployment:
#
#     MARKETFLOW_MANDATE_CAPITAL_USD=50000000
#
# A mandate may always tighten. Only a tier change widens a ceiling, and a tier is
# assigned out of band, never by the trading process.
MANDATE_CAPITAL_USD = float(os.environ.get("MARKETFLOW_MANDATE_CAPITAL_USD") or 1_000_000.0)

MANDATE_TIERS: dict[str, dict[str, float]] = {
    # tier           ceilings as a fraction of mandate capital
    "standard":     {"total": 0.50, "per_trade": 0.02, "drawdown": 0.10},
    "professional": {"total": 1.00, "per_trade": 0.05, "drawdown": 0.20},
}
DEFAULT_MANDATE_TIER = "standard"

# Opening defaults for a mandate that has not set its own caps yet. Deliberately
# far below the tier ceiling: a newly provisioned mandate trades small until
# somebody states a number.
MANDATE_DEFAULT_TOTAL_FRACTION = 0.10
MANDATE_DEFAULT_PER_TRADE_FRACTION = 0.005
MANDATE_DEFAULT_DRAWDOWN_FRACTION = 0.02

TENANT_DEFAULT_TOTAL_USD = MANDATE_CAPITAL_USD * MANDATE_DEFAULT_TOTAL_FRACTION
TENANT_DEFAULT_PER_TRADE_USD = MANDATE_CAPITAL_USD * MANDATE_DEFAULT_PER_TRADE_FRACTION
TENANT_DEFAULT_DRAWDOWN_USD = MANDATE_CAPITAL_USD * MANDATE_DEFAULT_DRAWDOWN_FRACTION

TENANT_CAP_CEILING_TOTAL_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["standard"]["total"]
TENANT_CAP_CEILING_PER_TRADE_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["standard"]["per_trade"]
TENANT_CAP_CEILING_DRAWDOWN_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["standard"]["drawdown"]
PRIME_CAP_CEILING_TOTAL_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["professional"]["total"]
PRIME_CAP_CEILING_PER_TRADE_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["professional"]["per_trade"]
PRIME_CAP_CEILING_DRAWDOWN_USD = MANDATE_CAPITAL_USD * MANDATE_TIERS["professional"]["drawdown"]


def tenant_caps(entry: dict[str, Any] | None) -> Any:
    """Build this mandate's FuseCaps from its saved rules: stated values clamped
    only by the tier's fat-finger ceilings. Missing or invalid values fall back to
    the conservative opening defaults, never to the ceiling. The tier is assigned
    out of band (entry["mandate_tier"]); the trading path can read it but never
    set it."""
    pmx = _pmx()
    rules = entry.get("rules") if isinstance(entry, dict) and isinstance(entry.get("rules"), dict) else {}
    tier = str(entry.get("mandate_tier") or "") if isinstance(entry, dict) else ""
    prime = tier == "professional"

    def _val(key: str, default: float) -> float:
        v = pmx.to_float(rules.get(key))
        return v if v is not None and v > 0 else default

    return pmx.FuseCaps(
        max_total_deploy_usd=_val("max_total_usd", TENANT_DEFAULT_TOTAL_USD),
        max_per_trade_usd=_val("max_per_trade_usd", TENANT_DEFAULT_PER_TRADE_USD),
        max_drawdown_usd=_val("max_drawdown_usd", TENANT_DEFAULT_DRAWDOWN_USD),
        ceiling_total_usd=PRIME_CAP_CEILING_TOTAL_USD if prime else TENANT_CAP_CEILING_TOTAL_USD,
        ceiling_per_trade_usd=PRIME_CAP_CEILING_PER_TRADE_USD if prime else TENANT_CAP_CEILING_PER_TRADE_USD,
        ceiling_drawdown_usd=PRIME_CAP_CEILING_DRAWDOWN_USD if prime else TENANT_CAP_CEILING_DRAWDOWN_USD,
    )

SELL_DECISIONS = ("STOP_LOSS_SELL", "TAKE_PROFIT_SELL", "SELL_SIGNAL", "TRIM_SELL_SIGNAL")

# --- automated entry --------------------------------------------------------
# Named sources only. A mandate's money may be committed by signal sources the
# deployment has explicitly registered and by nothing else. A new source is added
# deliberately, with its own evidence, rather than inheriting the capability from
# a sibling. Empty means no automated entry at all, which is the default: a fresh
# install cannot open a position until somebody names what is allowed to.
#
#     MARKETFLOW_ENTRY_SOURCES=desk_alpha_v3,settlement_arb
ENTRY_SOURCE_ALLOWLIST = tuple(
    s.strip() for s in os.environ.get("MARKETFLOW_ENTRY_SOURCES", "").split(",") if s.strip()
)
ARM_MODE_ENTRY_FLB = "entry_flb"

# Execution policies. A mandate picks a name; these map it onto parameters.
# Absolute exposure stays governed by the mandate's own caps — a policy decides
# HOW those caps get used, never how large they are. `per_trade_pct` is the share
# of the mandate's remaining deployable capital a single order may take, so these
# numbers carry across book sizes unchanged.
#
# `max_entry_price` is a structural bound, not a preference. Near-certain tickets
# invert the payoff geometry: the downside is the whole premium and the upside is
# a few percent, so the price band a policy may enter is capped independently of
# how aggressive the mandate wants to be. Raising the ceiling is a deployment
# decision that belongs in code review, not in a per-mandate setting.
RISK_PROFILES: dict[str, dict[str, float]] = {
    "conservative": {"per_trade_pct": 0.05, "max_new_positions_per_day": 2, "max_entry_price": 0.85},
    "balanced":     {"per_trade_pct": 0.10, "max_new_positions_per_day": 5, "max_entry_price": 0.85},
    "aggressive":   {"per_trade_pct": 0.20, "max_new_positions_per_day": 10, "max_entry_price": 0.90},
}
DEFAULT_RISK_PROFILE = "balanced"
# Hard ceiling on any policy's entry price. Even a hand-edited registry cannot
# push a mandate past it.
MAX_ENTRY_PRICE_CEILING = 0.90
# Headroom between sizing and fill, absorbing price drift and rounding. Without it
# every order is built at exactly the balance and rejected for insufficient funds.
ENTRY_BALANCE_HEADROOM = 0.98


def risk_profile(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve a tenant's risk profile to concrete parameters. Unknown/missing
    name falls back to the conservative-side default, never to aggressive."""
    rules = entry.get("rules") if isinstance(entry, dict) and isinstance(entry.get("rules"), dict) else {}
    name = str(rules.get("risk_profile") or DEFAULT_RISK_PROFILE).strip().lower()
    prof = dict(RISK_PROFILES.get(name) or RISK_PROFILES[DEFAULT_RISK_PROFILE])
    if name not in RISK_PROFILES:
        name = DEFAULT_RISK_PROFILE
    prof["max_entry_price"] = min(float(prof["max_entry_price"]), MAX_ENTRY_PRICE_CEILING)
    prof["name"] = name
    return prof


class _Timeout(Exception):
    pass


def _with_hard_timeout(seconds: float, label: str, fn: Callable[[], Any]) -> Any:
    """SIGALRM wall-clock bound on an idempotent network build (daemon pattern). SecureClient.create runs several serial CLOB reads that can hang;
    a stuck build raises rather than wedging the tick. Main-thread only — the
    service tick always runs there. NEVER wraps execute_order (a posted order is
    not signal-interruptible)."""

    def _raise(_sig: int, _frm: Any) -> None:
        raise _Timeout(f"{label} exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _pmx() -> Any:
    from marketflow.execution import orders as pmx  # type: ignore
    return pmx


def build_client_for_tenant(
    secrets: dict[str, Any],
    *,
    secret_dir: str,
    tenant_id: str | None = None,
    side: str | None = None,
    maker_amount_base_units: int | None = None,
) -> Any:
    """Build this tenant's SecureClient on whichever key backend it is on.

    `turnkey`: the private key never leaves Turnkey's enclave and the credential
    this process holds can only sign CLOB orders under a policy-enforced notional
    cap — so a compromised Guardian cannot transfer funds out, though it could
    still trade the account badly (marketflow/guardian/turnkey.py carries both the evidence
    and the limits). `local` (default): the historical path, key decrypted from
    the tenant's Fernet blob.

    Dispatch reads `key_backend` and nothing else. It is deliberately ONE-WAY: a
    Turnkey-backed tenant with the fleet gate shut refuses to trade rather than
    reverting to its local key, because reverting would hand back exactly the
    fund-moving capability the migration removed — and it would take only write
    access to a runtime directory to trigger.
    """
    from marketflow.guardian import turnkey as gturnkey

    if gturnkey.tenant_is_turnkey_backed(secrets):
        gturnkey.assert_turnkey_path_available(secrets)
        if gturnkey.tenant_is_user_root(secrets):
            from marketflow.guardian import authority as gauth

            if not tenant_id:
                raise gauth.AuthorityError("user-root client requires tenant identity")
            record = gauth.load_authority(tenant_id)
            summary = gauth.validate_user_root_authority(
                record,
                expected_tenant_id=tenant_id,
                side=side,
                expected_signer_address=secrets.get("turnkey_signer_address"),
                expected_funder_address=secrets.get("funder_address"),
                maker_amount_base_units=maker_amount_base_units,
            )
            if summary["suborg_id"] != str(secrets.get("turnkey_organization_id") or ""):
                raise gauth.AuthorityError("authority suborg does not match encrypted tenant secret")
            prefix = "turnkey_entry_agent" if side == "BUY" else "turnkey_exit_agent"
            public_key = str(secrets.get(f"{prefix}_public_key") or "")
            if public_key.lower() != str(summary["agent"]["api_public_key"]).lower():
                raise gauth.AuthorityError("authority agent does not match encrypted tenant secret")
        return gturnkey.build_turnkey_client(secrets, secret_dir=secret_dir, side=side)
    return _pmx().build_secure_client(secrets, secret_dir=secret_dir)


def tenant_arm_file(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(tenant_id), "arm_state.json")


def tenant_kill_file(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(tenant_id), "EXECUTION_KILL")


def tenant_ledger_file(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(tenant_id), "ledger.jsonl")


def plan_exit_for_decision(
    tenant_id: str,
    decision: dict[str, Any],
    position: dict[str, Any],
    *,
    requested_live: bool = True,
    book: Any = None,
    caps: Any = None,
) -> dict[str, Any] | None:
    """Build + fuse-gate an EXIT order for a SELL-side decision. Returns the S1
    plan dict (mode LIVE_PLAN / DRY_RUN_PLAN) or None if the decision is not a
    sell. Pure planning — never signs. Live is additionally gated below by
    GUARDIAN_LIVE_ENABLED, so in v0 this always plans DRY_RUN."""
    if decision.get("decision") not in SELL_DECISIONS:
        return None
    pmx = _pmx()
    token_id = decision.get("token_id") or position.get("token_id")
    held = pmx.to_float(position.get("held_shares"))
    if not token_id or not held or held <= 0:
        return None
    # Exit sizing: TRIM sells a fraction (half-Kelly convention from the monitor
    # brain, surfaced on the decision); a full exit sells all held shares. Worst
    # price floor = the current break-even/bid so a market SELL can't be dumped.
    frac = pmx.to_float(decision.get("trim_fraction")) if decision.get("decision") == "TRIM_SELL_SIGNAL" else 1.0
    frac = frac if (frac and 0 < frac <= 1.0) else (0.5 if decision.get("decision") == "TRIM_SELL_SIGNAL" else 1.0)
    size = round(held * frac, 6)
    min_price = pmx.to_float(position.get("current_sell_price") or position.get("break_even_probability")) or pmx.MIN_LIMIT_PRICE
    intent = pmx.build_exit_intent(
        token_id=str(token_id),
        held_shares=held,
        order_kind="market",
        size=size,
        min_price=max(pmx.MIN_LIMIT_PRICE, round(min_price * 0.98, 4)),
        market_slug=decision.get("market_slug"),
        signal_id=f"{tenant_id}:{decision.get('rule_fired')}:{token_id}",
        idempotency_key=f"{tenant_id}:{decision.get('rule_fired')}:{token_id}",
        note=decision.get("reason"),
    )
    live_gate = bool(requested_live and gstore.live_enabled())
    plan = pmx.plan_order(
        intent,
        requested_live=live_gate,
        # User-set caps under the tenant fat-finger ceiling; conservative starter
        # defaults when unset. Never owner_fuse_caps (that path is the owner's own stack).
        caps=caps if caps is not None else tenant_caps(None),
        kill_file=tenant_kill_file(tenant_id),
        arm_state_file=tenant_arm_file(tenant_id),
        ledger_path=tenant_ledger_file(tenant_id),
        book=book,
        arm_expected_writer=GUARDIAN_ARM_WRITER,
        arm_expected_ack=GUARDIAN_LIVE_ACK,
        arm_expected_tenant=tenant_id,
    )
    plan["guardian"] = {
        "tenant_id": tenant_id,
        "live_enabled_globally": gstore.live_enabled(),
        "exit_fraction": frac,
    }
    return plan


def tenant_collateral_usd(funder_address: str | None, fetcher: Callable[[str], Any] | None = None) -> float | None:
    """A hosted wallet's spendable Polymarket collateral (pUSD), read from public
    chain data — no credentials, no signing.

    Sizing needs the real balance, not just the caps: caps say how much a tenant is
    ALLOWED to deploy, the balance says what they actually HAVE. Sizing off caps
    alone builds every order at the cap and the exchange rejects it (observed on
    the owner account 260725, ). Returns None when unknown, and callers then
    keep their cap-only behaviour with the exchange as backstop — a balance lookup
    must never be able to block trading."""
    addr = str(funder_address or "").strip()
    if not addr:
        return None
    try:
        from marketflow.guardian import funding_watcher as fw  # noqa: E402  (sibling; owns the chain reader)
        # Balance comes from an on-chain balanceOf call, never from an indexer
        # snapshot: indexer token-balances have been measured lagging the chain by
        # the better part of an hour, and an inflated balance makes sizing keep
        # spending money that is already gone. The fetcher argument exists so the
        # selftest can inject one.
        if fetcher is not None:
            return fetcher(addr)
        return fw.pusd_balance_onchain(addr)
    except Exception:
        return None


def plan_entry_for_intent(
    tenant_id: str,
    intent_row: dict[str, Any],
    *,
    entry: dict[str, Any] | None = None,
    requested_live: bool = True,
    caps: Any = None,
    available_usd: float | None = None,
    opened_today: int = 0,
    book: Any = None,
) -> dict[str, Any] | None:
    """Build + fuse-gate a BUY for one allow-listed signal. Pure planning — never
    signs. Returns an S1 plan dict, or None when this tenant must not act on it.

    Every gate below is a reason to NOT trade; none of them can create permission.
    Order matters: the cheap structural refusals come before any network read."""
    pmx = _pmx()
    source = str((intent_row or {}).get("source") or "")
    if source not in ENTRY_SOURCE_ALLOWLIST:
        return None
    if not gstore.entry_enabled():
        return None
    prof = risk_profile(entry)
    if opened_today >= int(prof["max_new_positions_per_day"]):
        return None
    token_id = str(intent_row.get("token_id") or "")
    if not token_id:
        return None

    caps = caps if caps is not None else tenant_caps(entry)
    # Price off the CURRENT book, never the price frozen into the signal when it
    # was emitted — the emitter runs on its own schedule and everything below keys
    # off this number. A stale ask makes post-only orders cross the live book.
    quote = _live_quote(token_id, book=book)
    ask = quote.get("best_ask")
    if ask is None:
        ask = pmx.to_float(intent_row.get("ask_price"))
    if ask is None or not (0 < ask < 1):
        return None
    intent_max = pmx.to_float(intent_row.get("max_price"))
    if intent_max is not None and ask > intent_max + 1e-9:
        return None                      # price ran past what the signal authorised
    if ask > float(prof["max_entry_price"]) + 1e-9:
        return None                      # outside this profile's evidence-backed band
    # Structural traps. Nobody is at the keyboard on this path, so there is no one
    # to take an override — an enforced rule simply refuses.
    trap = gtraps.screen_buy(price=ask, tenant_id=tenant_id, entry=entry,
                             context="automated_entry")
    if trap["blocked"]:
        return None

    # Same market-quality gate as the owner stack (single source of truth), with
    # the profile's band as the ceiling.
    from marketflow.execution import market_gate as mgate  # noqa: E402
    gate = mgate.market_quality_gate(
        entry_ask_price=ask,
        model_probability=intent_row.get("model_probability"),
        resolution_confirmed_clean=intent_row.get("resolution_confirmed_clean") is True,
        auto_band_high=float(prof["max_entry_price"]),
    )
    if gate["authorization"] != "APPROVED":
        return None

    # Three ceilings: the tenant's per-trade cap, what the caps leave, and what the
    # wallet actually holds. The profile's per_trade_pct scales within them.
    ceilings = [
        float(caps.max_per_trade_usd),
        float(caps.max_total_deploy_usd) * float(prof["per_trade_pct"]),
    ]
    if available_usd is not None and available_usd >= 0:
        ceilings.append(float(available_usd) * ENTRY_BALANCE_HEADROOM)
    spend = min(ceilings)
    # Post-only maker price one tick inside the ask so the order cannot cross.
    tick = pmx.to_float(quote.get("tick_size")) or 0.01
    bid = pmx.to_float(quote.get("best_bid"))
    limit_px = round(bid + tick, 4) if (bid and bid + tick < ask) else round(ask - tick, 4)
    limit_px = max(pmx.MIN_LIMIT_PRICE, min(pmx.MAX_LIMIT_PRICE, limit_px))
    if limit_px <= 0 or limit_px >= 1:
        return None
    shares = float(int(spend / limit_px))
    min_shares = pmx.to_float(intent_row.get("order_min_size")) or 5.0
    if shares < min_shares:
        return None                      # cannot meet the exchange minimum within budget
    notional = round(shares * limit_px, 6)

    intent = pmx.build_entry_intent(
        token_id=token_id,
        order_kind="limit",
        price=limit_px,
        size=shares,
        max_price=limit_px,
        post_only=True,
        market_id=intent_row.get("market_id"),
        market_slug=intent_row.get("market_slug"),
        outcome=intent_row.get("side"),
        signal_id=f"{tenant_id}:{source}:{token_id}",
        idempotency_key=f"{tenant_id}:{intent_row.get('idempotency_key') or token_id}",
        settlement_cycle=str(intent_row.get("close_time") or ""),
        note=intent_row.get("reason"),
    )
    plan = pmx.plan_order(
        intent,
        requested_live=bool(requested_live and gstore.live_enabled() and gstore.entry_enabled()),
        caps=caps,
        kill_file=tenant_kill_file(tenant_id),
        arm_state_file=tenant_arm_file(tenant_id),
        ledger_path=tenant_ledger_file(tenant_id),
        book=book,
        arm_expected_writer=GUARDIAN_ARM_WRITER,
        arm_expected_ack=GUARDIAN_LIVE_ACK,
        arm_expected_tenant=tenant_id,
    )
    # S1 already refuses a BUY unless the arm mode permits it. This second, explicit
    # check pins the mode to entry_flb specifically: S1 would also accept "full",
    # and a hosted tenant must never be trading on a blanket authorisation even if
    # an arm file were hand-edited to one.
    if plan.get("will_execute_live"):
        arm_mode = str((plan.get("fuses") or {}).get("arm_mode") or "")
        if arm_mode != ARM_MODE_ENTRY_FLB:
            plan["will_execute_live"] = False
            plan["mode"] = "DRY_RUN_PLAN"
            plan["guardian_entry_refusal"] = (
                f"arm mode {arm_mode!r} is not {ARM_MODE_ENTRY_FLB!r}; entry forced to dry_run")
    # Carry the exact planned intent through to signing. Rebuilding it at execute
    # time risks signing something subtly different from what the fuses approved.
    # Leading underscore = not part of the serialisable plan record.
    plan["_intent_object"] = intent
    plan["guardian"] = {
        "tenant_id": tenant_id,
        "source": source,
        "market_id": intent_row.get("market_id"),
        "market_slug": intent_row.get("market_slug"),
        "risk_profile": prof["name"],
        "live_enabled_globally": gstore.live_enabled(),
        "entry_enabled_globally": gstore.entry_enabled(),
        "ask_price": ask,
        "ask_price_source": "live_book" if quote.get("best_ask") is not None else "signal_stale",
        "traps_tripped": trap["tripped"],
        "available_usd": available_usd,
        "spend_ceiling_usd": round(spend, 6),
        "estimated_notional_usd": notional,
        "opened_today": opened_today,
    }
    return plan


def _live_quote(token_id: str, *, book: Any = None) -> dict[str, Any]:
    """Current best bid/ask from the public CLOB book. Read-only, no auth.
    Fail-soft: {} on any error, and the caller keeps whatever price it had."""
    pmx = _pmx()
    try:
        raw = book if book is not None else pmx.fetch_book_readonly(str(token_id))
        bids = pmx.normalize_book_levels(raw, "bids")
        asks = pmx.normalize_book_levels(raw, "asks")
        out: dict[str, Any] = {
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
        }
        tick = pmx.to_float(pmx.attr(raw, "tick_size"))
        if tick and tick > 0:
            out["tick_size"] = tick
        return out
    except Exception:
        return {}


def execute_exit_plan(
    tenant_id: str,
    intent_plan: dict[str, Any],
    intent: Any,
    *,
    master_key: bytes | None = None,
) -> dict[str, Any]:
    """LIVE exit: only reached when a plan is live-cleared. Decrypts the tenant's
    secrets in-memory, builds an EOA SecureClient, executes through S1, records to
    the tenant ledger. Fails closed (returns a dry-run-equivalent record) on any
    missing secret / SDK unavailability."""
    pmx = _pmx()
    if not intent_plan.get("will_execute_live"):
        return {**intent_plan, "executed": False, "reason": "not live-cleared; dry_run"}
    tdir = gstore.tenant_dir(tenant_id)
    secrets = gwallet.decrypt_secrets(tdir, master_key=master_key)
    client = _with_hard_timeout(
        CLIENT_BUILD_TIMEOUT_SEC, "guardian live SecureClient build",
        lambda: build_client_for_tenant(
            secrets, secret_dir=tdir, tenant_id=tenant_id, side="SELL",
        ),
    )
    try:
        record = pmx.execute_order(
            client, intent, intent_plan,
            expected_wallet_type=EXPECTED_WALLET_TYPE,
            ledger_path=tenant_ledger_file(tenant_id),
            secret_dir=tdir,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    leaks = pmx.assert_no_secret_leak(record, secrets)
    if leaks:
        pmx.engage_global_halt("guardian secret leak guard tripped after live exit")
        record["secret_leak_guard"] = {"leaks": sorted(leaks), "halt_engaged": True}
    gstore.audit("exit_executed", tenant_id=tenant_id,
                 token_id=intent_plan.get("intent", {}).get("token_id"),
                 executed=bool(record.get("executed")))
    return record


def execute_entry_plan(
    tenant_id: str,
    intent_plan: dict[str, Any],
    intent: Any = None,
    *,
    master_key: bytes | None = None,
) -> dict[str, Any]:
    """LIVE entry: only reached when a plan is live-cleared. Mirrors
    `execute_exit_plan` — same secret handling, same S1 execution, same leak guard.

    The plan carries the exact intent it gated, so the signed order is the one the
    fuses approved; rebuilding it here would risk signing something subtly
    different from what was checked. Re-verifies the entry gate immediately before
    signing: a plan can be built and then the fleet gate pulled, and the last
    check before committing a hosted user's money should be the current one."""
    pmx = _pmx()
    if not intent_plan.get("will_execute_live"):
        return {**intent_plan, "executed": False, "reason": "not live-cleared; dry_run"}
    if not (gstore.live_enabled() and gstore.entry_enabled()):
        return {**intent_plan, "executed": False,
                "reason": "entry gate closed between planning and signing; refusing"}
    if intent is None:
        intent = intent_plan.get("_intent_object")
    if intent is None:
        return {**intent_plan, "executed": False,
                "reason": "no planned intent object to sign; refusing to rebuild"}
    tdir = gstore.tenant_dir(tenant_id)
    secrets = gwallet.decrypt_secrets(tdir, master_key=master_key)
    from marketflow.guardian import turnkey as gturnkey

    user_root = gturnkey.tenant_is_user_root(secrets)
    guardian = intent_plan.get("guardian") or {}
    reservation_id = str(guardian.get("risk_reservation_id") or "")
    market_id = str(guardian.get("market_id") or "")
    notional = float(guardian.get("estimated_notional_usd") or 0.0)
    if user_root:
        from marketflow.guardian import risk_budget as grisk

        if not reservation_id:
            raise grisk.RiskBudgetError("user-root BUY has no active risk reservation")
        grisk.validate_reservation(
            tenant_id, reservation_id, market_id=market_id, notional_usd=notional,
        )
    try:
        client = _with_hard_timeout(
            CLIENT_BUILD_TIMEOUT_SEC, "guardian live SecureClient build",
            lambda: build_client_for_tenant(
                secrets,
                secret_dir=tdir,
                tenant_id=tenant_id,
                side="BUY",
                maker_amount_base_units=max(1, int(round(notional * 1_000_000))),
            ),
        )
    except Exception:
        if user_root and reservation_id:
            grisk.release_entry(
                tenant_id, reservation_id, reason="refused_before_client_ready",
            )
        raise
    try:
        record = pmx.execute_order(
            client, intent, intent_plan,
            expected_wallet_type=EXPECTED_WALLET_TYPE,
            ledger_path=tenant_ledger_file(tenant_id),
            secret_dir=tdir,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    if user_root and reservation_id:
        if record.get("executed") is True:
            # An accepted resting maker order consumes the budget even before it
            # fills; otherwise a loop could stack many live orders under one cap.
            grisk.commit_entry(
                tenant_id, reservation_id, filled_notional_usd=notional,
            )
        else:
            grisk.release_entry(
                tenant_id, reservation_id, reason="venue_rejected_order",
            )
    leaks = pmx.assert_no_secret_leak(record, secrets)
    if leaks:
        pmx.engage_global_halt("guardian secret leak guard tripped after live entry")
        record["secret_leak_guard"] = {"leaks": sorted(leaks), "halt_engaged": True}
    gstore.audit("entry_executed", tenant_id=tenant_id,
                 source=(intent_plan.get("guardian") or {}).get("source"),
                 market_slug=(intent_plan.get("guardian") or {}).get("market_slug"),
                 executed=bool(record.get("executed")))
    return record
