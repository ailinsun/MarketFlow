#!/usr/bin/env python3
"""Multi-tenant non-custodial decision loop (dry-run / research skeleton).

Proves N accounts can each run position-management rules under their OWN caps and
rules in full isolation, without any tenant's config or failure touching another
tenant or the operator's own stack. It is the layer that decides, per account,
what to do -- never the layer that holds a key or moves money.

HARD BOUNDARIES (this module never crosses them):
  - Never signs / posts / cancels / reads any private key. It produces per-tenant
    DECISIONS + ALERTS only. Real execution stays behind the existing single-tenant
    arm-state / caps / kill fuses (the order module); this layer
    can only ADD per-tenant tightening, never widen a fuse.
  - the operator's own live daemon files (their secret_dir / arm_state_file / ledger) are
    never read or written here. Each tenant lives in its own file namespace under
    `runtime/execution/tenants/<tenant_id>/`.
  - fail-closed: unknown mode / missing credential ref / malformed tenant config
    forces alert_only + dry_run and is isolated; it never escalates to live and
    never aborts the other tenants.

Semantics (tokens, caps, fuses) follow the execution module's definitions.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable, Optional


HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
# decide_position is a pure function: the same graduated de-risk / take-profit
# brain the operator's own monitor uses. Per-tenant rules only pick its kwargs.
from marketflow.risk import positions as monitor
from marketflow.execution import orders as pmx

# Settlement guard (fail-closed on dirty settlement). Pure read-only modules from
# the monitor package (market metadata + on-chain UMA proposal direction ->
# settlement-cleanliness verdict). Import is best-effort so a missing module
# degrades to "no settlement gate", never crashes the loop.
try:
    from marketflow.monitor import settlement_guard as sguard
    from marketflow.monitor import uma_onchain as uma_chain
    from marketflow.monitor import polymarket_data as alerts_pmd
except Exception:  # pragma: no cover - guard is optional
    sguard = None  # type: ignore
    uma_chain = None  # type: ignore
    alerts_pmd = None  # type: ignore


TENANTS_ROOT = runtime_path("execution", "tenants")
DEFAULT_REGISTRY = os.path.join(TENANTS_ROOT, "registry.json")
DEFAULT_SELFTEST = os.path.join(TENANTS_ROOT, "selftest_report.json")
# Shared BUY-only breaker: one file halts entries for EVERY tenant at once, on
# top of each tenant's own kill file. Same path the single-tenant stack uses.
GLOBAL_HALT_FILE = pmx.DEFAULT_GLOBAL_HALT_FILE

REGISTRY_SCHEMA_VERSION = "polymarket-tenant-registry-v0.1"
TENANT_SCHEMA_VERSION = "polymarket-tenant-v0.1"
DECISION_SCHEMA_VERSION = "polymarket-tenant-decision-v0.1"

TENANT_MODES = ("alert_only", "auto")
# The channel a deployment's own delivery process should use for a tenant's
# alerts. Nothing here delivers: every alert is written to the tenant's own alert
# ledger with its channel name and a ready-to-send text, and delivery (and its
# credentials) stays outside this module.
ALERT_CHANNELS = ("none", "email", "webhook")

# Public data plane: any address's positions and any market's book are
# public information (on-chain / public APIs). Reading them needs ZERO
# credentials — that is what makes alert-only non-custodial by construction.
DATA_API_BASE = "https://data-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
PUBLIC_HTTP_TIMEOUT_SEC = 20.0
MAX_POSITIONS_PER_TENANT = 50
BOOK_THROTTLE_SEC = 0.25  # be polite to the public book endpoint

class TenantError(Exception):
    """Raised for malformed tenant/registry input (always isolated, never fatal)."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_obj(obj: Any) -> str:
    return sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def write_json(path: str, data: Any) -> None:
    ensure_parent(path)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)


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


def _safe_str(value: Any, *, max_len: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_len] if text else None


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _sanitize_tenant_id(raw: Any) -> str:
    """Tenant id must be a filesystem-safe slug (it names a directory)."""
    text = _safe_str(raw, max_len=64) or ""
    slug = "".join(ch for ch in text if ch.isalnum() or ch in ("-", "_"))
    if not slug or slug != text:
        raise TenantError(f"tenant_id must be a non-empty [A-Za-z0-9_-] slug (got {raw!r})")
    return slug


@dataclass
class TenantRules:
    """Per-account position rules. Compose with the shared `decide_position`
    rules: the explicit stop-loss / take-profit gate fires first (what the account
    set), then the graduated model logic fills the rest."""

    entry_enabled: bool = False
    # Explicit exits set per account, as fraction of entry cost (0.20 == 20%). None == off.
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    # decide_position knobs (shared rules). Defaults mirror the single-account stack.
    risk_buffer: float = 0.03
    edge_buffer: float = 0.05
    kelly_fraction: float = 0.5
    prob_sd: float = 0.05
    market_cap_frac: float = 0.40

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "TenantRules":
        d = d if isinstance(d, dict) else {}

        def _fpct(key: str) -> Optional[float]:
            v = _to_float(d.get(key))
            if v is None:
                return None
            # A percent rule outside (0, 5] is almost certainly a typo; fail-closed
            # by dropping it rather than acting on a nonsense threshold.
            return v if 0.0 < v <= 5.0 else None

        return cls(
            entry_enabled=bool(d.get("entry_enabled", False)),
            stop_loss_pct=_fpct("stop_loss_pct"),
            take_profit_pct=_fpct("take_profit_pct"),
            risk_buffer=_to_float(d.get("risk_buffer")) or 0.03,
            edge_buffer=_to_float(d.get("edge_buffer")) or 0.05,
            kelly_fraction=_to_float(d.get("kelly_fraction")) or 0.5,
            prob_sd=_to_float(d.get("prob_sd")) or 0.05,
            market_cap_frac=_to_float(d.get("market_cap_frac")) or 0.40,
        )


@dataclass
class TenantConfig:
    """One account. Everything is per-tenant and isolated; nothing here can widen a
    module-level fuse (caps clamp through pmx.FuseCaps, which only tightens)."""

    tenant_id: str
    mode: str
    public_wallet: Optional[str]
    caps: pmx.FuseCaps
    rules: TenantRules
    alert_channel: str
    # Auto-mode only; alert_only never references a credential at all.
    secret_dir: Optional[str] = None
    # Derived isolated file namespace (never overlaps the operator's own stack).
    root: str = ""
    degraded_reason: Optional[str] = None

    @property
    def arm_state_file(self) -> str:
        return os.path.join(self.root, "arm_state.json")

    @property
    def kill_file(self) -> str:
        return os.path.join(self.root, "EXECUTION_KILL")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.root, "daemon.lock")

    @property
    def ledger_file(self) -> str:
        return os.path.join(self.root, "ledger.jsonl")

    @property
    def alerts_file(self) -> str:
        return os.path.join(self.root, "alerts.jsonl")

    @property
    def latest_file(self) -> str:
        return os.path.join(self.root, "latest.json")

    def public_view(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "mode": self.mode,
            "public_wallet": pmx.mask_address(self.public_wallet) if self.public_wallet else None,
            "caps": {
                "max_total_deploy_usd": self.caps.max_total_deploy_usd,
                "max_per_trade_usd": self.caps.max_per_trade_usd,
            },
            "rules": {
                "entry_enabled": self.rules.entry_enabled,
                "stop_loss_pct": self.rules.stop_loss_pct,
                "take_profit_pct": self.rules.take_profit_pct,
                "risk_buffer": self.rules.risk_buffer,
                "edge_buffer": self.rules.edge_buffer,
            },
            "alert_channel": self.alert_channel,
            "has_credentials": bool(self.secret_dir),
            "degraded_reason": self.degraded_reason,
        }


def load_tenant(raw: dict, *, tenants_root: str = TENANTS_ROOT) -> TenantConfig:
    """Parse one tenant, fail-closed. Any doubt downgrades toward alert_only /
    dry_run; a credential ref that does not resolve strips auto mode."""
    if not isinstance(raw, dict):
        raise TenantError("tenant entry is not an object")
    tenant_id = _sanitize_tenant_id(raw.get("tenant_id"))

    mode = str(raw.get("mode") or "alert_only").strip().lower()
    if mode not in TENANT_MODES:
        mode = "alert_only"
    degraded_reason: Optional[str] = None

    # Caps clamp through the bound module ceiling: a tenant can ask for less than
    # the default fractions but never more (pmx.FuseCaps.__post_init__ min()-clamps).
    caps_in = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
    caps = pmx.FuseCaps(
        max_total_deploy_usd=_to_float(caps_in.get("max_total_deploy_usd")) or pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD,
        max_per_trade_usd=_to_float(caps_in.get("max_per_trade_usd")) or pmx.DEFAULT_MAX_PER_TRADE_USD,
    )

    alert_channel = str(raw.get("alert_channel") or "none").strip().lower()
    if alert_channel not in ALERT_CHANNELS:
        alert_channel = "none"

    secret_dir: Optional[str] = None
    if mode == "auto":
        secret_dir = _safe_str(raw.get("secret_dir"), max_len=512)
        # fail-closed: auto with no resolvable credential ref cannot execute; it is
        # downgraded to alert_only so the loop keeps running WITHOUT ever holding
        # or expecting a key it does not have.
        if not secret_dir or not os.path.isdir(secret_dir):
            degraded_reason = "auto mode requested but secret_dir missing/unresolved; downgraded to alert_only"
            mode = "alert_only"
            secret_dir = None

    root = os.path.join(tenants_root, tenant_id)
    return TenantConfig(
        tenant_id=tenant_id,
        mode=mode,
        public_wallet=_safe_str(raw.get("public_wallet"), max_len=64),
        caps=caps,
        rules=TenantRules.from_dict(raw.get("rules")),
        alert_channel=alert_channel,
        secret_dir=secret_dir,
        root=root,
        degraded_reason=degraded_reason,
    )


def load_registry(path: str = DEFAULT_REGISTRY, *, tenants_root: str = TENANTS_ROOT) -> tuple[list[TenantConfig], list[dict]]:
    """Load all tenants. A malformed tenant is skipped + recorded; it can never
    crash the run or leak into another tenant (isolation starts at parse time)."""
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("tenants"), list):
        raise TenantError("registry must be an object with a 'tenants' list")
    tenants: list[TenantConfig] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for entry in data["tenants"]:
        try:
            tenant = load_tenant(entry, tenants_root=tenants_root)
            if tenant.tenant_id in seen:
                raise TenantError(f"duplicate tenant_id {tenant.tenant_id}")
            seen.add(tenant.tenant_id)
            tenants.append(tenant)
        except Exception as exc:  # isolate a bad tenant from the good ones
            skipped.append({"entry": entry, "error": f"{type(exc).__name__}: {exc}"})
    return tenants, skipped


def _global_halt_active() -> bool:
    return os.path.exists(GLOBAL_HALT_FILE)


# ---------------------------------------------------------------------------
# Public data plane (zero credentials)
# ---------------------------------------------------------------------------

def _http_get_json(url: str, *, timeout: float = PUBLIC_HTTP_TIMEOUT_SEC) -> Any:
    """GET a public endpoint. Honors env http(s)_proxy (urllib default). Only
    ever used against public, credential-free APIs."""
    req = urllib.request.Request(url, headers={"User-Agent": "marketflow-position-manager/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_public_positions(wallet: str, *, limit: int = MAX_POSITIONS_PER_TENANT) -> list[dict[str, Any]]:
    """Read a wallet's open positions from the PUBLIC Data API (on-chain-derived,
    zero credentials — anyone can read any address). Maps to the position dict
    `evaluate_position` consumes. Raises on transport error (caller isolates)."""
    q = urllib.parse.urlencode({"user": wallet, "limit": int(limit), "sizeThreshold": 1})
    rows = _http_get_json(f"{DATA_API_BASE}/positions?{q}")
    if not isinstance(rows, list):
        raise TenantError(f"positions endpoint returned {type(rows).__name__}, expected list")
    return [_map_position_row(row) for row in rows if isinstance(row, dict)]


def _map_position_row(row: dict[str, Any]) -> dict[str, Any]:
    """Pure mapping: Data-API position row -> the dict evaluate_position eats."""
    cur = _to_float(row.get("curPrice"))
    return {
        "token_id": _safe_str(row.get("asset"), max_len=240),
        "condition_id": _safe_str(row.get("conditionId"), max_len=80),
        "market_slug": _safe_str(row.get("slug"), max_len=260),
        "title": _safe_str(row.get("title"), max_len=300),
        "outcome": _safe_str(row.get("outcome"), max_len=40),
        "outcomeIndex": (int(row["outcomeIndex"])
                         if str(row.get("outcomeIndex", "")).strip().lstrip("-").isdigit() else None),
        "endDate": _safe_str(row.get("endDate"), max_len=40),
        "held_shares": _to_float(row.get("size")),
        "entry_price": _to_float(row.get("avgPrice")),
        "current_sell_price": cur,
        "unrealized_pnl_pct": (_to_float(row.get("percentPnl")) or 0.0) / 100.0
                               if row.get("percentPnl") is not None else None,
        "current_value_usd": _to_float(row.get("currentValue")),
        "redeemable": row.get("redeemable") is True,
        # Model-brain fields are filled by book enrichment; without a book the
        # explicit stop-loss/take-profit rules still work off pnl alone.
        "break_even_probability": cur,
        "full_liquidity": False,
        "marketflow_p": None,
    }


def fetch_best_bid(token_id: str) -> Optional[dict[str, Any]]:
    """Best bid + bid depth from the public CLOB book. Returns None on any
    failure (closed market 404s) — caller falls back to explicit-rule-only."""
    try:
        q = urllib.parse.urlencode({"token_id": token_id})
        book = _http_get_json(f"{CLOB_API_BASE}/book?{q}")
        bids = book.get("bids") if isinstance(book, dict) else None
        if not bids:
            return None
        # bids are sorted ascending; the last entry is the best (highest) bid.
        best = bids[-1]
        price = _to_float(best.get("price"))
        depth = sum(_to_float(b.get("size")) or 0.0 for b in bids)
        if price is None:
            return None
        return {"best_bid": price, "bid_depth_shares": depth}
    except Exception:
        return None


def public_position_source(tenant: TenantConfig) -> list[dict[str, Any]]:
    """The public position source: public positions for the tenant's public
    wallet, enriched with live book bids where the market is still open. A
    tenant with no wallet configured simply has no positions (fail-closed)."""
    if not tenant.public_wallet:
        return []
    positions = fetch_public_positions(tenant.public_wallet)
    for pos in positions:
        if pos.get("redeemable"):
            continue  # settled; book is gone, redemption alert handles it
        token = pos.get("token_id")
        if not token:
            continue
        book = fetch_best_bid(token)
        if book is not None:
            held = pos.get("held_shares") or 0.0
            pos["break_even_probability"] = book["best_bid"]
            pos["current_sell_price"] = book["best_bid"]
            pos["full_liquidity"] = book["bid_depth_shares"] >= held > 0
        _enrich_settlement(pos)
        time.sleep(BOOK_THROTTLE_SEC)
    return positions


def _enrich_settlement(pos: dict[str, Any]) -> None:
    """Attach `pos['settlement']` = settlement-cleanliness assessment (read-only:
    gamma market metadata + on-chain UMA proposal). Best-effort: if the sidecar is
    missing or any fetch fails the field is simply absent and the guard is a no-op.

    Shape mapped to what settlement_guard.assess_settlement consumes (it reads the
    raw gamma fields: outcomes / outcomePrices / umaResolutionStatuses / resolvedBy
    / description / endDate, plus the position's outcome/outcomeIndex/endDate)."""
    if sguard is None or alerts_pmd is None:
        return
    cid = pos.get("condition_id")
    if not cid:
        return
    try:
        metas = alerts_pmd.fetch_market_meta([cid])
    except Exception:  # noqa: BLE001 - guard is best-effort, never crashes the loop
        return
    meta = metas.get(cid)
    uma = None
    if meta is not None and uma_chain is not None:
        statuses = alerts_pmd.market_dispute_status(meta).get("statuses")
        if statuses:
            uma = uma_chain.fetch_uma_proposal(
                meta.get("resolvedBy"),
                question_id=meta.get("questionID"),
                neg_risk_request_id=meta.get("negRiskRequestID"),
            )
    guard_pos = {
        "outcome": pos.get("outcome"),
        "outcomeIndex": pos.get("outcomeIndex"),
        "endDate": pos.get("endDate"),
        "redeemable": pos.get("redeemable"),
        "title": pos.get("title"),
    }
    try:
        pos["settlement"] = sguard.assess_settlement(guard_pos, meta, uma)
    except Exception:  # noqa: BLE001
        return


def _unrealized_pnl_pct(position: dict) -> Optional[float]:
    """PnL vs entry cost, as a signed fraction (+0.5 == up 50%). Prefer an
    explicit field; else derive from entry vs current sale price."""
    explicit = _to_float(position.get("unrealized_pnl_pct"))
    if explicit is not None:
        return explicit
    entry = _to_float(position.get("entry_price"))
    sell = _to_float(position.get("current_sell_price") or position.get("break_even_probability"))
    if entry and entry > 0 and sell is not None:
        return round((sell - entry) / entry, 8)
    return None


def evaluate_position(tenant: TenantConfig, position: dict) -> dict[str, Any]:
    """Per-tenant decision for one held position, dry-run only, then gated by the
    settlement guard.

    Order of authority:
      1) the account's explicit stop-loss / take-profit
      2) the shared graduated rules (`decide_position`) for everything else
      3) the SETTLEMENT GUARD (fail-closed): if the market's settlement mechanics
         are not clean (UMA propose/dispute, mispriced proposal, non-standard
         resolver), any automated action is HELD and an alert is recorded instead.
    Output is a DECISION + optional alert payload — never an order.
    """
    decision = _decide_position(tenant, position)
    return _apply_settlement_guard(decision, position)


def _apply_settlement_guard(decision: dict[str, Any], position: dict) -> dict[str, Any]:
    """Fail-closed gate: on a not-clean settlement, downgrade any actionable
    trading decision to SETTLEMENT_GUARD_HOLD (alert-only, no automated action).

    A legitimately-settled REDEEMABLE_CLAIM and pure OBSERVE_ONLY are never gated —
    the guard only holds decisions that would otherwise move a position while the
    settlement itself is untrustworthy. No sidecar / no settlement data => no gate."""
    if sguard is None:
        return decision
    settlement = position.get("settlement")
    if not isinstance(settlement, dict):
        return decision
    if decision.get("decision") not in _GUARDED_DECISIONS:
        return decision
    verdict = sguard.guard_verdict(settlement)
    return sguard.guard_decision(decision, verdict)


def _decide_position(tenant: TenantConfig, position: dict) -> dict[str, Any]:
    """The ungated per-tenant decision (rules + graduated brain). Pure."""
    rules = tenant.rules
    pnl = _unrealized_pnl_pct(position)
    base = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "generated_at": iso_now(),
        "tenant_id": tenant.tenant_id,
        "mode": tenant.mode,
        "token_id": _safe_str(position.get("token_id"), max_len=240),
        "market_slug": _safe_str(position.get("market_slug"), max_len=260),
        "unrealized_pnl_pct": pnl,
        "dry_run": True,
    }

    # 0) a settled position cannot be traded, only redeemed — surface it before
    # any trading rule (stop-loss on a settled market is meaningless).
    if position.get("redeemable") is True:
        value = _to_float(position.get("current_value_usd"))
        return {**base, "decision": "REDEEMABLE_CLAIM",
                "reason": "market settled; position is redeemable on Polymarket"
                          + (f" (residual value ${value:.2f})" if value else ""),
                "rule_fired": "redeemable"}

    # 1) explicit user exits fire first.
    if rules.stop_loss_pct is not None and pnl is not None and pnl <= -rules.stop_loss_pct:
        return {**base, "decision": "STOP_LOSS_SELL",
                "reason": f"unrealized {pnl:+.2%} <= stop-loss -{rules.stop_loss_pct:.0%}",
                "rule_fired": "stop_loss"}
    if rules.take_profit_pct is not None and pnl is not None and pnl >= rules.take_profit_pct:
        return {**base, "decision": "TAKE_PROFIT_SELL",
                "reason": f"unrealized {pnl:+.2%} >= take-profit +{rules.take_profit_pct:.0%}",
                "rule_fired": "take_profit"}

    # 2) graduated model brain for the rest (shared pure function).
    graded = monitor.decide_position(
        marketflow_p=_to_float(position.get("marketflow_p")),
        break_even_probability=_to_float(position.get("break_even_probability")),
        risk_buffer=rules.risk_buffer,
        full_liquidity=bool(position.get("full_liquidity", True)),
        held_shares=_to_float(position.get("held_shares")),
        prev_peak_price=_to_float(position.get("prev_peak_price")),
        kelly_fraction=rules.kelly_fraction,
        prob_sd=rules.prob_sd,
        bankroll_usd=_to_float(position.get("bankroll_usd")),
        best_ask=_to_float(position.get("best_ask")),
        edge_buffer=rules.edge_buffer,
        market_cap_frac=rules.market_cap_frac,
    )
    return {**base, "decision": graded.get("decision", "OBSERVE_ONLY"),
            "reason": graded.get("reason"), "rule_fired": "graduated_model",
            "model": {k: v for k, v in graded.items() if k not in ("decision", "reason")}}


# Decisions the settlement guard may hold when settlement is not clean. A settled
# REDEEMABLE_CLAIM and pure OBSERVE_ONLY are intentionally excluded — the guard
# holds actions that MOVE a live position, not a redemption or a no-op.
_GUARDED_DECISIONS = (
    "STOP_LOSS_SELL", "TAKE_PROFIT_SELL", "SELL_SIGNAL", "TRIM_SELL_SIGNAL",
    "ENTRY_SIGNAL",
)

ACTIONABLE_DECISIONS = (
    "STOP_LOSS_SELL", "TAKE_PROFIT_SELL", "SELL_SIGNAL", "TRIM_SELL_SIGNAL",
    "ENTRY_SIGNAL", "REDEEMABLE_CLAIM", "SETTLEMENT_GUARD_HOLD",
)


def _alert_text(tenant: TenantConfig, decision: dict) -> str:
    """Human-readable alert text: what happened + what to do. Never contains a
    credential or a full wallet address."""
    slug = decision.get("market_slug") or decision.get("token_id") or "position"
    pnl = decision.get("unrealized_pnl_pct")
    pnl_txt = f" ({pnl:+.1%})" if isinstance(pnl, (int, float)) else ""
    action = {
        "STOP_LOSS_SELL": "🔴 Stop-loss hit — consider selling",
        "TAKE_PROFIT_SELL": "🟢 Take-profit hit — consider selling",
        "SELL_SIGNAL": "🔴 Exit signal — position looks -EV",
        "TRIM_SELL_SIGNAL": "🟡 Trim signal — consider partial sell",
        "ENTRY_SIGNAL": "🔵 Entry signal",
        "REDEEMABLE_CLAIM": "💰 Market settled — redeem your position",
        "SETTLEMENT_GUARD_HOLD": "🛡️ Settlement guard — automated action held",
    }.get(str(decision.get("decision")), str(decision.get("decision")))
    return f"{action}\n{slug}{pnl_txt}\n{decision.get('reason') or ''}\nhttps://polymarket.com/market/{slug}"


def _emit_alert(tenant: TenantConfig, decision: dict) -> Optional[dict]:
    """Turn an actionable decision into a per-tenant alert row in the tenant's own
    alert ledger, carrying the channel name and a ready-to-send text for the
    deployment's delivery process. This layer never sends money, never signs and
    never delivers. Each (tenant, token, decision) alert fires once (dedup by key
    against the tenant's own alert ledger)."""
    if decision.get("decision") not in ACTIONABLE_DECISIONS:
        return None
    alert_key = hash_obj({
        "tenant": tenant.tenant_id,
        "token": decision.get("token_id"),
        "decision": decision.get("decision"),
        "rule": decision.get("rule_fired"),
    })[:24]
    if _alert_already_sent(tenant.alerts_file, alert_key):
        return None
    alert = {
        "schema_version": "polymarket-tenant-alert-v0.1",
        "generated_at": iso_now(),
        "tenant_id": tenant.tenant_id,
        "alert_key": alert_key,
        "channel": tenant.alert_channel,
        "decision": decision.get("decision"),
        "reason": decision.get("reason"),
        "token_id": decision.get("token_id"),
        "market_slug": decision.get("market_slug"),
        "text": _alert_text(tenant, decision),
    }
    append_jsonl(tenant.alerts_file, alert)
    return alert


def _alert_already_sent(alerts_file: str, alert_key: str) -> bool:
    """One alert per (tenant, token, decision, rule) — a watch loop must not
    re-spam the same signal every tick. Reading the tenant's own small ledger
    keeps dedup crash-safe with no extra state file."""
    if not os.path.exists(alerts_file):
        return False
    try:
        with open(alerts_file, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("alert_key") == alert_key:
                    return True
    except OSError:
        return False
    return False


def run_tenant(tenant: TenantConfig, positions: list[dict]) -> dict[str, Any]:
    """Run one tenant's full tick in ISOLATION. Any exception is caught and
    written to this tenant's own record; it never propagates to other tenants."""
    result: dict[str, Any] = {
        "schema_version": "polymarket-tenant-run-v0.1",
        "generated_at": iso_now(),
        "tenant": tenant.public_view(),
        "global_halt_active": _global_halt_active(),
        "decisions": [],
        "alerts": [],
        "error": None,
    }
    try:
        for position in positions:
            decision = evaluate_position(tenant, position)
            # Global HALT is a BUY-only breaker: it suppresses new-entry alerts for
            # every tenant at once, but never blocks a SELL / stop-loss exit.
            if result["global_halt_active"] and decision.get("decision") == "ENTRY_SIGNAL":
                decision = {**decision, "decision": "OBSERVE_ONLY",
                            "reason": "global HALT active; entries suppressed for all tenants (exits still allowed)",
                            "rule_fired": "global_halt"}
            result["decisions"].append(decision)
            append_jsonl(tenant.ledger_file, decision)
            alert = _emit_alert(tenant, decision)
            if alert is not None:
                result["alerts"].append(alert)
    except Exception as exc:  # isolation boundary
        result["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "traceback": traceback.format_exc(limit=4)}
    write_json(tenant.latest_file, result)
    return result


def run_all(
    registry_path: str = DEFAULT_REGISTRY,
    *,
    positions_by_tenant: Optional[dict[str, list[dict]]] = None,
    position_source: Optional[Callable[[TenantConfig], list[dict]]] = None,
    tenants_root: str = TENANTS_ROOT,
) -> dict[str, Any]:
    """Run every tenant, each fully isolated. positions_by_tenant (or a
    position_source callback) supplies per-tenant held positions; in this
    dry-run skeleton they are injected rather than read from live accounts."""
    tenants, skipped = load_registry(registry_path, tenants_root=tenants_root)
    runs: list[dict] = []
    for tenant in tenants:
        if positions_by_tenant is not None:
            positions = positions_by_tenant.get(tenant.tenant_id, [])
        elif position_source is not None:
            try:
                positions = position_source(tenant)
            except Exception as exc:  # a bad source for one tenant is isolated
                positions = []
                runs.append({"tenant_id": tenant.tenant_id, "position_source_error": str(exc)})
        else:
            positions = []
        runs.append(run_tenant(tenant, positions))
    return {
        "schema_version": "polymarket-multitenant-run-v0.1",
        "generated_at": iso_now(),
        "registry_path": registry_path,
        "tenant_count": len(tenants),
        "skipped": skipped,
        "global_halt_active": _global_halt_active(),
        "runs": runs,
    }


def selftest() -> dict[str, Any]:
    """Prove the multi-tenant loop: same input, different per-user rules ->
    different per-user decisions, each in its own namespace; and one tenant's
    failure does not stop another."""
    ensure_parent(DEFAULT_SELFTEST)
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="tenants_", dir=TENANTS_ROOT if os.path.isdir(TENANTS_ROOT) else None) as tmp:
        os.makedirs(tmp, exist_ok=True)
        registry = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "tenants": [
                {  # tight stop-loss, tight take-profit
                    "tenant_id": "alpha",
                    "mode": "alert_only",
                    "public_wallet": "0xAAAA000000000000000000000000000000000001",
                    "alert_channel": "webhook",
                    # asks for less than the module default -> kept as asked
                    "caps": {"max_total_deploy_usd": pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD / 10,
                             "max_per_trade_usd": pmx.DEFAULT_MAX_PER_TRADE_USD / 10},
                    "rules": {"stop_loss_pct": 0.10, "take_profit_pct": 0.30},
                },
                {  # looser stop-loss -> same position does NOT trip it
                    "tenant_id": "beta",
                    "mode": "auto",  # no secret_dir -> fail-closed downgrade to alert_only
                    "public_wallet": "0xBBBB000000000000000000000000000000000002",
                    "alert_channel": "email",
                    # asks for more than the bound module default -> clamped down
                    "caps": {"max_total_deploy_usd": pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD * 10,
                             "max_per_trade_usd": pmx.DEFAULT_MAX_PER_TRADE_USD * 10},
                    "rules": {"stop_loss_pct": 0.40, "take_profit_pct": 0.90},
                },
                {  # malformed -> skipped, must not crash the run
                    "tenant_id": "bad id with spaces",
                    "mode": "auto",
                },
            ],
        }
        reg_path = os.path.join(tmp, "registry.json")
        write_json(reg_path, registry)

        # Same synthetic position for BOTH tenants: down 20% from entry.
        position = {
            "token_id": "tok_selftest",
            "market_slug": "selftest-market",
            "entry_price": 0.50,
            "current_sell_price": 0.40,  # -20% vs entry
            "break_even_probability": 0.40,
            "marketflow_p": 0.42,
            "held_shares": 100.0,
            "full_liquidity": True,
        }
        out = run_all(
            reg_path,
            positions_by_tenant={"alpha": [position], "beta": [position]},
            tenants_root=tmp,
        )

        by_id = {r.get("tenant", {}).get("tenant_id"): r for r in out["runs"] if r.get("tenant")}
        alpha = by_id.get("alpha", {})
        beta = by_id.get("beta", {})
        alpha_dec = (alpha.get("decisions") or [{}])[0].get("decision")
        beta_dec = (beta.get("decisions") or [{}])[0].get("decision")

        checks["registry_loaded_two_valid"] = out["tenant_count"] == 2
        checks["malformed_tenant_skipped"] = len(out["skipped"]) == 1
        checks["alpha_stop_loss_fires_at_minus20"] = alpha_dec == "STOP_LOSS_SELL"
        checks["beta_looser_stop_loss_does_not_fire"] = beta_dec != "STOP_LOSS_SELL"
        checks["same_input_different_decision"] = alpha_dec != beta_dec
        checks["beta_auto_downgraded_fail_closed"] = beta.get("tenant", {}).get("mode") == "alert_only"
        checks["beta_caps_clamped_to_ceiling"] = (
            beta.get("tenant", {}).get("caps", {}).get("max_total_deploy_usd") == pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD
        )
        checks["alpha_alert_written"] = len(alpha.get("alerts") or []) == 1
        checks["per_tenant_isolated_namespace"] = (
            os.path.isdir(os.path.join(tmp, "alpha")) and os.path.isdir(os.path.join(tmp, "beta"))
        )
        checks["no_tenant_error"] = alpha.get("error") is None and beta.get("error") is None

        # Isolation under failure: a tenant whose position makes decide_position
        # raise must not stop the other tenant from completing.
        boom = TenantConfig(
            tenant_id="boom", mode="alert_only", public_wallet=None,
            caps=pmx.FuseCaps(), rules=TenantRules(), alert_channel="none",
            root=os.path.join(tmp, "boom"),
        )
        good = TenantConfig(
            tenant_id="good", mode="alert_only", public_wallet=None,
            caps=pmx.FuseCaps(), rules=TenantRules(stop_loss_pct=0.10), alert_channel="none",
            root=os.path.join(tmp, "good"),
        )

        class _Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("injected position fault")

        boom_run = run_tenant(boom, [_Boom()])
        good_run = run_tenant(good, [position])
        checks["failing_tenant_isolated"] = boom_run.get("error") is not None
        checks["healthy_tenant_completes_despite_neighbor"] = (
            good_run.get("error") is None and (good_run.get("decisions") or [{}])[0].get("decision") == "STOP_LOSS_SELL"
        )

        # --- public-data pieces (all offline) ---
        # Data-API row mapping: the public schema as the venue returns it.
        api_row = {
            "asset": "1000000001", "conditionId": "0xc0de", "size": 1250.0,
            "avgPrice": 0.48, "curPrice": 0.36, "currentValue": 450.0,
            "cashPnl": -150.0, "percentPnl": -25.0,
            "title": "T", "outcome": "Yes", "slug": "some-market", "redeemable": False,
        }
        mapped = _map_position_row(api_row)
        checks["api_row_maps_entry_and_shares"] = (
            mapped["entry_price"] == 0.48 and mapped["held_shares"] == 1250.0
        )
        checks["api_row_pnl_pct_is_fraction"] = abs(mapped["unrealized_pnl_pct"] - (-0.25)) < 1e-9
        checks["api_row_no_book_means_no_model_brain"] = mapped["full_liquidity"] is False

        # Settled position -> redemption reminder outranks every trading rule.
        redeem_row = _map_position_row({**api_row, "redeemable": True, "curPrice": 0,
                                        "currentValue": 1250.0, "percentPnl": 108.33})
        redeem_dec = evaluate_position(good, redeem_row)
        checks["settled_position_redeem_alert"] = redeem_dec["decision"] == "REDEEMABLE_CLAIM"

        # Alert dedup: the same signal does not re-fire on the next tick.
        dedup_t = TenantConfig(
            tenant_id="dedup", mode="alert_only", public_wallet=None,
            caps=pmx.FuseCaps(), rules=TenantRules(stop_loss_pct=0.10), alert_channel="none",
            root=os.path.join(tmp, "dedup"),
        )
        first = run_tenant(dedup_t, [position])
        second = run_tenant(dedup_t, [position])
        checks["alert_fires_once"] = len(first.get("alerts") or []) == 1
        checks["alert_row_carries_text_not_delivery"] = (
            bool((first.get("alerts") or [{}])[0].get("text"))
            and "delivered" not in (first.get("alerts") or [{}])[0])
        checks["alert_deduped_on_next_tick"] = len(second.get("alerts") or []) == 0

        # Onboarding CLI path: add validates through load_tenant; dup refused.
        reg2 = os.path.join(tmp, "reg2.json")
        add_tenant_to_registry(reg2, tenant_id="gamma", public_wallet="0xC",
                               stop_loss_pct=0.2, alert_channel="webhook")
        try:
            add_tenant_to_registry(reg2, tenant_id="gamma", public_wallet="0xC")
            checks["add_tenant_dup_refused"] = False
        except TenantError:
            checks["add_tenant_dup_refused"] = True
        added, _skip2 = load_registry(reg2, tenants_root=tmp)
        checks["added_tenant_roundtrips"] = (
            len(added) == 1 and added[0].alert_channel == "webhook"
            and added[0].rules.stop_loss_pct == 0.2
        )

        # --- settlement guard (fail-closed) ---
        if sguard is not None:
            # A stop-loss-tripping position whose settlement is DIRTY (disputed) must
            # be HELD, not auto-sold. Same position with a CLEAN settlement sells.
            dirty = {**position, "settlement": {"clean": False, "reasons": ["uma_disputed"],
                                                "phase": "disputed"}}
            gd = evaluate_position(good, dirty)
            checks["guard_holds_action_on_dirty_settlement"] = (
                gd["decision"] == "SETTLEMENT_GUARD_HOLD"
                and gd.get("suppressed_decision") == "STOP_LOSS_SELL"
            )
            clean = {**position, "settlement": {"clean": True, "reasons": [], "phase": "window"}}
            gc = evaluate_position(good, clean)
            checks["guard_passes_action_on_clean_settlement"] = gc["decision"] == "STOP_LOSS_SELL"
            # No settlement data at all => guard is a no-op (never blocks blindly).
            gn = evaluate_position(good, position)
            checks["guard_noop_without_settlement_data"] = gn["decision"] == "STOP_LOSS_SELL"
            # The held decision surfaces as an alert (user is told we did NOT act).
            guard_t = TenantConfig(
                tenant_id="guard", mode="alert_only", public_wallet=None,
                caps=pmx.FuseCaps(), rules=TenantRules(stop_loss_pct=0.10), alert_channel="none",
                root=os.path.join(tmp, "guard"),
            )
            grun = run_tenant(guard_t, [dirty])
            checks["guard_hold_emits_alert"] = any(
                a.get("decision") == "SETTLEMENT_GUARD_HOLD" for a in (grun.get("alerts") or [])
            )
            # A REDEEMABLE settled claim is never gated (redeeming is safe).
            redeem_dirty = {**_map_position_row({**api_row, "redeemable": True}),
                            "settlement": {"clean": False, "reasons": ["uma_disputed"]}}
            checks["guard_never_gates_redemption"] = (
                evaluate_position(good, redeem_dirty)["decision"] == "REDEEMABLE_CLAIM"
            )

    report = {
        "schema_version": "polymarket-multitenant-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": all(checks.values()),
        "checks": checks,
    }
    write_json(DEFAULT_SELFTEST, report)
    return report


def add_tenant_to_registry(
    registry_path: str,
    *,
    tenant_id: str,
    public_wallet: Optional[str],
    mode: str = "alert_only",
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    alert_channel: str = "none",
) -> dict[str, Any]:
    """Onboarding = one command. Validates through the same fail-closed
    load_tenant parser before persisting; a broken entry never lands."""
    entry = {
        "tenant_id": tenant_id,
        "mode": mode,
        "public_wallet": public_wallet,
        "alert_channel": alert_channel,
        "rules": {k: v for k, v in (("stop_loss_pct", stop_loss_pct),
                                    ("take_profit_pct", take_profit_pct)) if v is not None},
    }
    load_tenant(entry)  # raises TenantError on anything malformed
    if os.path.exists(registry_path):
        with open(registry_path, encoding="utf-8") as f:
            registry = json.load(f)
        if not isinstance(registry, dict) or not isinstance(registry.get("tenants"), list):
            raise TenantError("existing registry is malformed; refusing to overwrite")
    else:
        registry = {"schema_version": REGISTRY_SCHEMA_VERSION, "tenants": []}
    if any(isinstance(t, dict) and t.get("tenant_id") == tenant_id for t in registry["tenants"]):
        raise TenantError(f"tenant_id {tenant_id} already registered")
    registry["tenants"].append(entry)
    write_json(registry_path, registry)
    return entry


def watch(registry_path: str, *, interval_sec: float = 60.0,
          max_ticks: Optional[int] = None) -> int:
    """Resident loop: every tick, read each tenant's PUBLIC positions and
    run their rules. One tenant failing (bad wallet, API hiccup) is isolated by
    run_tenant / run_all; the loop itself only exits on signal."""
    ticks = 0
    while True:
        out = run_all(registry_path, position_source=public_position_source)
        summary = {
            "generated_at": out["generated_at"],
            "tenants": out["tenant_count"],
            "alerts": sum(len(r.get("alerts") or []) for r in out["runs"]),
            "errors": sum(1 for r in out["runs"] if r.get("error")),
        }
        print(canonical_json(summary), flush=True)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return 0
        time.sleep(max(5.0, interval_sec))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket multi-tenant non-custodial position manager (public data only).")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--run", action="store_true", help="one tick over live PUBLIC positions for all tenants")
    parser.add_argument("--watch", action="store_true", help="resident loop")
    parser.add_argument("--once", action="store_true", help="one watch tick then exit (for an external scheduler)")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--smoke", metavar="WALLET", help="end-to-end dry read of one public wallet (no registry write)")
    parser.add_argument("--add-tenant", metavar="TENANT_ID")
    parser.add_argument("--wallet")
    parser.add_argument("--mode", default="alert_only", choices=list(TENANT_MODES))
    parser.add_argument("--stop-loss", type=float, default=None, help="fraction of entry cost, e.g. 0.2")
    parser.add_argument("--take-profit", type=float, default=None)
    parser.add_argument("--channel", default="none", choices=list(ALERT_CHANNELS))
    args = parser.parse_args(argv)

    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["PASS"] else 1

    if args.smoke:
        probe = TenantConfig(
            tenant_id="smoke", mode="alert_only", public_wallet=args.wallet or args.smoke,
            caps=pmx.FuseCaps(), rules=TenantRules(stop_loss_pct=args.stop_loss,
                                                   take_profit_pct=args.take_profit),
            alert_channel="none", root=os.path.join(TENANTS_ROOT, "_smoke"),
        )
        positions = public_position_source(probe)
        decisions = [evaluate_position(probe, p) for p in positions]
        print(json.dumps({"wallet_masked": pmx.mask_address(args.smoke),
                          "positions": len(positions), "decisions": decisions},
                         ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.add_tenant:
        entry = add_tenant_to_registry(
            args.registry, tenant_id=args.add_tenant, public_wallet=args.wallet,
            mode=args.mode, stop_loss_pct=args.stop_loss, take_profit_pct=args.take_profit,
            alert_channel=args.channel)
        print(json.dumps({"added": entry}, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.once:
        return watch(args.registry, interval_sec=args.interval, max_ticks=1)

    if args.watch:
        return watch(args.registry, interval_sec=args.interval)

    if args.run:
        out = run_all(args.registry, position_source=public_position_source)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    tenants, skipped = load_registry(args.registry)
    print(json.dumps({"tenants": [t.public_view() for t in tenants], "skipped": skipped},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
