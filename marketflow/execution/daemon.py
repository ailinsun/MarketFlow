#!/usr/bin/env python3
"""Polymarket autonomous loop daemon v0.1 (S3).

This is the 24/7 orchestration layer for the owner's Polymarket auto-trade pivot
(forward-forecast protocol: every decision is logged before its outcome is known). It
stitches the chain into a loop that can run overnight:

    market data (CLOB orderbook, REST now / websocket later)
      + true win-rate (S2 sports/win-rate provider; fallbacks are explicit)
      -> critical-win-rate decision
      -> execution intent (S1 CLOB execution stack, guarded by arm-state/fuses)

This daemon prioritises EXIT MANAGEMENT: an already-held position whose true win-rate
falls below its immediate-sale break-even is auto-flagged for sell. That is the
exact gap where an unattended position goes unmanaged overnight. Autonomous ENTRY is a
guarded path controlled by arm-state, caps, and kill/HALT fuses.

Capital protection is a set of FUSES, not edge gates:
  - kill switch: a file or a signal halts all execution immediately;
  - dry_run by default: live execution needs BOTH --live AND an armed unified
    arm-state (the single-source control file written by a separate process on an owner
    frontend click; shared with S1). Its mode (off/exit_only/full) drives whether
    exits and/or entries are live;
  - capital caps: per-trade + total deployment caps gate ENTRIES (exits never);
  - structured JSONL ledger + optional file alerts.

Hard boundaries (never crossed by this daemon):
  - default dry_run; no live order without an armed arm-state;
  - no crypto paper.py, live kernel, state.mx, or WSTP/server control action;
  - autonomous thinking is engine-native: daemon may trigger the bridge queue,
    but never implements a bridge/daemon-side LLM fallback;
  - secrets are never printed or written to the ledger;
  - live execution is delegated only to S1's authoritative fuses; the daemon
    does not build SDK orders itself.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable, Optional


HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
from marketflow.risk import positions as monitor  # noqa: E402  (path-injected sibling module)
from marketflow.execution import orders as pmx  # noqa: E402  (S1 execution module; single-source arm-state + fuses)
from marketflow.feeds.rotation import append_jsonl_line as _rotating_append  # noqa: E402
from marketflow.execution import market_gate as mgate  # noqa: E402  (longshot/band/resolution/min-edge market-quality gate)
from marketflow.risk import resolution as rverify  # noqa: E402  (independent clean-resolution attestation; feeds the fuse, never bypasses it)
from marketflow.risk import sizing as possizer  # noqa: E402  (conservative-quantile Kelly chassis; entry sizing uses its fraction math)
from marketflow.risk import experience as trade_experience  # noqa: E402  (experience memory + playbook)

OUT_DIR = runtime_path("execution", "daemon")
DEFAULT_LEDGER = os.path.join(OUT_DIR, "ledger.jsonl")
DEFAULT_LATEST_JSON = os.path.join(OUT_DIR, "latest.json")
DEFAULT_LATEST_SUMMARY = os.path.join(OUT_DIR, "latest_summary.md")
DEFAULT_SELFTEST = os.path.join(OUT_DIR, "selftest_report.json")
DEFAULT_KILL_FILE = os.path.join(OUT_DIR, "KILL_SWITCH")
DEFAULT_LOCK_FILE = os.path.join(OUT_DIR, "daemon.pidlock")
DEFAULT_ALERT_FILE = os.path.join(OUT_DIR, "alerts.jsonl")
DEFAULT_EXAMPLE_CONFIG = os.path.join(OUT_DIR, "example_config.json")
DEFAULT_SECRET_DIR = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets")
DEFAULT_BRIDGE_AUTONOMOUS_URL = "http://127.0.0.1:3000/api/chat/autonomous"
DEFAULT_AGENT_INTENT_QUEUE = runtime_path("execution", "polymarket_agent_intents.jsonl")
# Single-source arm-state shared with S1 (polymarket_execution): the bridge is the
# only writer; this daemon and S1 only ever READ it. Retired the daemon's own
# .txt live_ack phrase in favor of this one file (single source of truth).
DEFAULT_ARM_STATE_FILE = pmx.DEFAULT_ARM_STATE_FILE


def validated_local_bridge_url(value: Any) -> str:
    """Allow the AI bridge only on a literal loopback address.

    The bridge may suggest intents, but it must never become a remote network
    capability adjacent to the money process.  Literal loopback avoids DNS and
    proxy ambiguity; the exact route prevents config-controlled URL expansion.
    """
    raw = str(value or DEFAULT_BRIDGE_AUTONOMOUS_URL).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid autonomous bridge URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not (1 <= port <= 65535)
        or parsed.path != "/api/chat/autonomous"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("autonomous bridge must be an exact loopback HTTP endpoint")
    return raw

SCHEMA_VERSION = "polymarket-autotrade-daemon-v0.1"
AGENT_INTENT_CONSUMED_SCHEMA_VERSION = "polymarket-agent-intent-consumed-v0.1"

DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_RISK_BUFFER = 0.03
DEFAULT_EDGE_BUFFER = 0.05
DEFAULT_FEE_BPS = 0.0
# Price-expression gate: "shadow" (compute + audit only) | "enforce" (refuse).
# Enforced. Shadow measurement showed zero behavioural change: a maker (post-only)
# entry pays no fee at all, so the fee term only bites inside an in-play taker
# window. Same direction as the execution layer's price floor — it tightens in one
# direction and never loosens.
DEFAULT_PRICE_EXPRESSION_MODE = "enforce"
DEFAULT_TOTAL_CAP_USD = pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD
DEFAULT_PER_TRADE_CAP_USD = pmx.DEFAULT_MAX_PER_TRADE_USD
DEFAULT_MARKET_QUALITY = {
    "longshot_low": 0.15,
    "longshot_high": 0.85,
    "auto_band_low": 0.15,   # inner band collapsed onto longshot guard (2026-06-24): trade
    "auto_band_high": 0.85,  # the whole non-deep-longshot range; edge managed post-entry, not pre-judged
    "min_edge_after_cost": 0.04,
    "cost_buffer": 0.02,
    "require_model_probability": True,
    "require_resolution": True,
    "edge_advisory": True,
}
DEFAULT_KELLY_FRACTION = 0.20
DEFAULT_ENTRY_TICK_SIZE = 0.01
# Post-only BUY pricing rule. "legacy" = cheapest maker price (best_bid + tick);
# "queue_aware" = most fillable price that still clears the edge ceiling (# fill model). Legacy is the default because live order prices are a money surface:
# switching costs one config key and one restart, but it is the owner's call, not a
# side effect of deploying this module.
DEFAULT_MAKER_PRICING = "legacy"
SPEED_WINDOW_TAKER_REASON = "in_play_speed_window"
SELF_ATTESTED_RESOLUTION_SOURCES = {"marketflow_chat", "autonomous_turn", "engine_native_tool"}

EXIT_DECISIONS_NO_ACTION = {
    "HOLD",
    "DO_NOTHING",
    "NONE",
    "OBSERVE_ONLY",
    "LIQUIDITY_BLOCKED",
    "MARKET_DATA_BLOCKED",
    "CONFIG_BLOCKED",
}

BOUNDARIES = [
    "default_dry_run",
    "no_live_order_without_armed_arm_state",
    "exit_first_then_gated_entry",
    "capital_caps_gate_entries_not_exits",
    "kill_switch_file_and_signal",
    "no_secret_print",
    "no_secret_ledger_write",
    "no_crypto_paper_py",
    "no_live_kernel",
    "no_state_mx",
    "no_wstpserver_control_action",
    "engine_native_autonomy_no_bridge_fallback",
    "live_execution_delegated_to_s1_authoritative_fuses",
]

POSITION_DISCOVERY_FAILED = object()


class DaemonError(Exception):
    """Raised for fail-loud daemon errors."""


def exception_payload(exc: BaseException, **extra: Any) -> dict:
    payload = dict(extra)
    payload["error"] = str(exc)
    payload["error_type"] = exc.__class__.__name__
    return payload


def _with_hard_timeout(seconds: float, label: str, fn: Callable[[], Any]) -> Any:
    """Run a blocking daemon step with a SIGALRM wall-clock timeout."""
    seconds_f = float(seconds or 0)
    if seconds_f <= 0:
        return fn()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _on_timeout(_signum: int, _frame: Any) -> None:
        raise DaemonError(f"{label} timed out after {seconds_f:.1f}s")

    signal.signal(signal.SIGALRM, _on_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds_f)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer and (old_timer[0] > 0 or old_timer[1] > 0):
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def parse_close_dt(close_time: Any) -> "datetime | None":
    """close_time -> an aware datetime; None when it cannot be parsed.

    Accepts both formats that occur in practice: an ISO string, and an epoch in
    seconds or milliseconds carried through from market metadata. This matters: a
    signal layer emitting epochs while the execution layer only parsed ISO once
    caused every autonomous entry to be refused as having a bad close time. The
    signal side was producing, the execution side was refusing everything, and the
    entire chain was stuck on a format mismatch."""
    s = str(close_time or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        ts = float(s)
    except ValueError:
        return None
    if ts > 1e11:  # ms epoch
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


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


def append_jsonl(path: str, row: dict) -> None:
    ensure_parent(path)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(canonical_json(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


DAEMON_ARCHIVE_ROOT = runtime_path("archive", "autotrade-daemon-daily")


def append_jsonl_rotating(path: str, row: dict) -> None:
    """Append to an unbounded per-tick stream (ledger, alerts): same durability as
    append_jsonl — exclusive lock plus fsync — with daily and size rotation so the
    active file stays bounded and older segments land gzipped under
    DAEMON_ARCHIVE_ROOT.

    Deliberately NOT used for the intent queues: _load_agent_intent_consumed_keys
    reads the consumed file whole to dedupe, so a rotated-away key would let a
    already-filled intent open a second position.
    """
    _rotating_append(path, canonical_json(row) + "\n",
                     archive_root=DAEMON_ARCHIVE_ROOT, fsync=True)


def _pending_intents_summary(queue_path: str = DEFAULT_AGENT_INTENT_QUEUE, *, limit: int = 25) -> list:
    """Read-only summary of the agent-intent queue minus consumed, mirrored into
    latest_json so the frontend shows which build orders are queued waiting for arm
    — independent of whether this tick consumes them. No side effects."""
    try:
        path = queue_path if os.path.isabs(queue_path) else os.path.join(REPO_ROOT, queue_path)
        if not os.path.isfile(path):
            return []
        consumed = _load_agent_intent_consumed_keys(_agent_intent_consumed_path(path))
        out: list = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                key = monitor.safe_str(row.get("idempotency_key"))
                if key and key in consumed:
                    continue
                out.append({
                    "side": row.get("side"),
                    "market_slug": row.get("market_slug") or row.get("market_id"),
                    "created_at": row.get("created_at") or row.get("as_of"),
                    "expires_at": row.get("expires_at"),
                    "max_spend_usd": row.get("max_spend_usd"),
                    "model_probability": row.get("model_probability"),
                })
        return out[-limit:]
    except Exception:
        return []


def _agent_intent_consumed_path(queue_path: str) -> str:
    base, _ext = os.path.splitext(os.path.abspath(queue_path))
    return base + "_consumed.jsonl"


CLEARED_HALTS_DIR = runtime_path("execution", "cleared_halts")
_RESERVE_HALT_MARKER = "reserve cap would be breached"
_ADMIN_CHAT_FILE = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "telegram_admin_chat.txt")


_TG_DIRECT_TIMEOUT_SEC = 6.0
_TG_ENV_PROXY_TIMEOUT_SEC = 8.0


def _redact_token(msg: str, token: str) -> str:
    """A failed alert must leave a trace, but a bot token embedded in a URL would go
    into the service log with it. Both the full string and the bare id segment are
    redacted (a token looks like `<id>:<secret>` and some errors echo only the id),
    which preserves the never-logs-the-token contract."""
    out = str(msg)
    if token:
        out = out.replace(token, "<TG_TOKEN>")
        head = token.split(":", 1)[0]
        if len(head) >= 6:
            out = out.replace(head, "<TG_TOKEN_ID>")
    return out


def _tg_openers() -> "list[tuple[urllib.request.OpenerDirector, float]]":
    """[(direct, timeout), (env proxy fallback, timeout)] — the order is the
    priority. Timeouts are returned paired with their opener rather than as a
    parallel sequence, so a length mismatch cannot silently truncate the fallback
    path in a zip.

    An alerting channel must not depend on the thing it monitors. A deployment that
    sets proxy environment variables for the execution chain gets them honoured by
    urllib **by default**, so the moment that tunnel dies the HALT and
    reserve-breach alerts cannot be sent either — and a dead tunnel is itself one of
    the things that triggers a HALT. Direct is tried first (measured several times
    faster than through a tunnel anyway) and the env proxy is the fallback."""
    return [(urllib.request.build_opener(urllib.request.ProxyHandler({})), _TG_DIRECT_TIMEOUT_SEC),
            (urllib.request.build_opener(), _TG_ENV_PROXY_TIMEOUT_SEC)]


def _tg_should_try_other_transport(exc: BaseException) -> bool:
    """Is a different transport worth trying? Only for a transport-layer failure.

    An HTTPError means the server answered, so the transport works and the failure
    is at the application layer: an invalid token, a chat that does not exist, rate
    limiting. Another route gets the same answer, and rate limiting is actively
    worse — it is applied per token regardless of source address, so retrying just
    hits it again. A connection error, timeout or DNS failure is the transport's
    business."""
    return not isinstance(exc, urllib.error.HTTPError)


def _internal_tg_target() -> "tuple[str, str]":
    """(bot token, target chat) for internal alerts. It never returns a credential
    belonging to a user-facing channel.

    The target is an operations group where colleagues can read it, falling back to
    a direct message when the group id is unknown — **still the same bot**. It reads
    files only and depends on no alerting module: this sits on the daemon's fuse
    path and must not acquire dependencies.
    """
    team_dir = os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "team_bots")
    token = ""
    for slug in ("aide", "true", "win", "peace"):
        path = os.path.join(team_dir, f"{slug}.txt")
        try:
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                break
        except OSError:
            continue
    if not token:
        return "", ""
    chat_id = ""
    try:  # the group id's source of truth is the daemon's own cached state
        state_path = runtime_path("monitor", "operator.json")
        with open(state_path, encoding="utf-8") as f:
            chat_id = str(json.load(f).get("operator_chat_id") or "")
    except (OSError, ValueError, AttributeError):
        chat_id = ""
    if not chat_id:
        try:
            with open(_ADMIN_CHAT_FILE, encoding="utf-8") as f:
                chat_id = f.read().strip()
        except OSError:
            chat_id = ""
    return token, chat_id


def _admin_telegram_notify(text: str) -> bool:
    """Fail-soft internal alert push, HALT lifecycle only.
    Reads local secret refs, never raises, never logs the token.

    A user-facing channel carries user-facing information and nothing else; not one
    internal message goes through it. The recipient is an operations group, falling
    back to a direct message on the same credential when the group id is unknown —
    a different inbox, never a different identity.

    **The transport layer below is deliberately untouched.** The opener gradient and
    timeout budget are part of the money fuses' visibility, so changing the
    destination must not disturb them.

    **Blocking budget**: worst case at the socket layer is the sum of the two
    timeouts. Note that name resolution happens **before** a socket timeout applies
    and is not bounded by it, so a sick resolver adds its own tail — and "the
    router is down" correlates with "the tunnel is down" rather than being
    independent of it.

    The budget that matters is not the tick length but the degraded-liveness
    threshold, and a tick already consumes most of it. What these seconds directly
    delay is an **exit** — SELL stays permitted during a HALT, and this notify runs
    before position discovery — which is why it has to be short.

    When both routes fail it **writes one line to stderr**. This is a fuse alert: if
    it cannot send itself, that fact has to be visible in the service log, or it
    becomes the other half of a silent failure — somebody seeing no alert and
    concluding nothing is wrong. The trace is redacted, preserving the
    never-logs-the-token contract."""
    token = ""
    try:
        token, chat_id = _internal_tg_target()
        if not token or not chat_id:
            print("[autotrade] operator notify skipped: no bot token or target",
                  file=sys.stderr, flush=True)
            return False
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        errors = []
        for opener, timeout in _tg_openers():
            try:
                # Any non-2xx is raised as an HTTPError before reaching this branch,
                # so there is no unreachable status case to write.
                with opener.open(urllib.request.Request(url, data=payload), timeout=timeout) as resp:
                    if 200 <= getattr(resp, "status", 0) < 300:
                        return True
            except Exception as exc:
                errors.append(f"HTTPError {exc.code}" if isinstance(exc, urllib.error.HTTPError)
                              else f"{type(exc).__name__}: {exc}")
                if not _tg_should_try_other_transport(exc):
                    break
        print("[autotrade] admin TG notify FAILED on both transports "
              f"(direct, env-proxy): {_redact_token(' | '.join(errors), token)[:300]}",
              file=sys.stderr, flush=True)
        return False
    except Exception as exc:
        print(f"[autotrade] admin TG notify FAILED: {_redact_token(f'{type(exc).__name__}: {exc}', token)[:300]}",
              file=sys.stderr, flush=True)
        return False


def _try_auto_clear_reserve_halt(*, min_age_sec: float = 1800.0) -> dict:
    """Auto-clear ONLY the reserve-breach flavor of the global HALT, and only after
    the squeeze that tripped it is provably gone (no unexpired pending BUY intent
    left in the queue). Clearing lifts the *global* ceasefire; every live BUY still
    passes S1's authoritative per-order budget precheck, so this can never spend
    past caps. unknown_fill and every other HALT flavor stays manual forever."""
    out: dict = {"cleared": False, "reason": None}
    try:
        halt_file = pmx.DEFAULT_GLOBAL_HALT_FILE
        if not os.path.exists(halt_file):
            out["reason"] = "no_halt"
            return out
        with open(halt_file, encoding="utf-8") as f:
            content = f.read().strip()
        out["halt_content"] = content
        if _RESERVE_HALT_MARKER not in content:
            out["reason"] = "not_reserve_flavor_manual_only"
            return out
        age = time.time() - os.path.getmtime(halt_file)
        if age < min_age_sec:
            out["reason"] = f"too_fresh_{int(age)}s"
            return out
        now_iso = iso_now()
        live_pending = [p for p in _pending_intents_summary(limit=1000)
                        if str(p.get("expires_at") or "") > now_iso]
        if live_pending:
            out["reason"] = f"pending_intents_alive_{len(live_pending)}"
            return out
        os.makedirs(CLEARED_HALTS_DIR, exist_ok=True)
        stamp = now_iso.replace("-", "").replace(":", "")
        dest = os.path.join(CLEARED_HALTS_DIR, f"HALT.{stamp}.auto_cleared_reserve_breach")
        os.replace(halt_file, dest)
        out.update({"cleared": True, "archived_to": dest, "halt_age_sec": round(age)})
        return out
    except Exception as exc:
        out["reason"] = f"error:{exc}"
        return out


def _load_agent_intent_consumed_keys(consumed_path: str) -> set[str]:
    keys: set[str] = set()
    if not os.path.isfile(consumed_path):
        return keys
    with open(consumed_path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            key = monitor.safe_str(row.get("idempotency_key"))
            if key:
                keys.add(key)
    return keys


def to_float(value: Any) -> Optional[float]:
    return monitor.to_float(value)


def minimum_buy_sizing(candidate: dict, *, entry_price: Optional[float], max_price: Optional[float]) -> dict:
    """Compute the market-specific minimum BUY size.

    Polymarket `order_min_size` is shares. Cash risk is therefore
    `order_min_size * executable price`, not a fixed dollar value. If that
    metadata is missing, fail closed instead of guessing.
    """
    min_size = to_float(candidate.get("order_min_size") or candidate.get("order_min_size_shares"))
    px = to_float(entry_price)
    worst_px = to_float(max_price if max_price is not None else entry_price)
    tick = to_float(candidate.get("tick_size"))
    if min_size is None or min_size <= 0:
        return {"ok": False, "reason": "missing order_min_size; refusing to guess minimum order"}
    if px is None or px <= 0 or worst_px is None or worst_px <= 0:
        return {"ok": False, "reason": "missing executable price for minimum order sizing"}
    shares = min_size
    return {
        "ok": True,
        "sizing_policy": "exchange_minimum_only",
        "order_min_size": round(min_size, 8),
        "tick_size": round(tick, 8) if tick is not None else None,
        "shares": round(shares, 8),
        "entry_price": round(px, 8),
        "max_price": round(worst_px, 8),
        "estimated_notional_usd": round(shares * px, 8),
        "max_spend_usd": round(shares * worst_px, 8),
    }


_BOOK_FETCH = None          # set at first use; self-tests replace it with a stub


def _book_fetch(token_id: str):
    """The order-book read, isolated behind a name a test can replace.

    Like the fee-rate read, this is fail-soft, so without the seam an offline
    self-test would quietly depend on the venue being reachable and the fallback
    would hide it.
    """
    return (_BOOK_FETCH or pmx.fetch_book_readonly)(token_id)


def _live_book_quote(token_id: Any) -> dict:
    """Current best bid/ask for a token from the public CLOB book. Read-only, no auth.

    Fail-soft by design: any error returns {} and the caller keeps whatever price
    it already had. A quote lookup must never be able to block or crash a tick."""
    tok = monitor.safe_str(token_id) if token_id is not None else ""
    if not tok:
        return {}
    try:
        book = _book_fetch(tok)
        bids = pmx.normalize_book_levels(book, "bids")
        asks = pmx.normalize_book_levels(book, "asks")
        out: dict = {
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            # Full depth, not just the top of book: queue-aware maker pricing needs
            # the size sitting at every level, and the fetch already paid for it.
            "bid_levels": bids,
            "ask_levels": asks,
        }
        tick = to_float(pmx.attr(book, "tick_size"))
        if tick and tick > 0:
            out["tick_size"] = tick
        return out
    except Exception:
        return {}


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_FEE_RATE_CACHE: dict[str, tuple[float, str, float]] = {}   # market_id -> (rate, source, fetched_at)
FEE_RATE_CACHE_TTL_SEC = 3600.0   # a market's feeSchedule does not move intraday


def _fetch_market_rows(url: str, timeout: float = 5.0) -> list:
    """The single network read in this module, isolated so it can be replaced.

    Self-tests swap it for a stub. Without the seam a self-test would depend on the
    venue being reachable, and a fail-soft fallback would hide that dependency
    instead of surfacing it.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "marketflow-autotrade-daemon"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_FEE_RATE_FETCH = _fetch_market_rows


def _market_fee_rate(market_id: Any, candidate: Optional[dict] = None) -> tuple[float, str]:
    """This market's taker fee rate + where it came from. requires the
    per-market `feeSchedule.rate`, never a hardcoded constant.

    Order of trust: a rate the candidate packet already carries > Gamma
    `feeSchedule.rate` (cached 1h) > `feesEnabled=false` means a genuinely
    zero-fee market > the measured modal fallback.

    Fail-soft like `_live_book_quote`: any error falls back and says so in the
    source label. The fallback is the CONSERVATIVE direction (a higher assumed
    fee only ever tightens an entry), so a Gamma outage can never loosen a gate.
    """
    if isinstance(candidate, dict):
        packet = to_float(candidate.get("fee_rate"))
        if packet is not None and packet >= 0:
            return packet, "candidate_packet"
    mid = monitor.safe_str(market_id) if market_id is not None else ""
    if not mid:
        return mgate.FEE_RATE_FALLBACK, "fallback"
    hit = _FEE_RATE_CACHE.get(mid)
    if hit is not None and (time.time() - hit[2]) < FEE_RATE_CACHE_TTL_SEC:
        return hit[0], hit[1]
    rate, source = mgate.FEE_RATE_FALLBACK, "fallback"
    try:
        url = f"{GAMMA_MARKETS_URL}?{urllib.parse.urlencode({'condition_ids': mid})}"
        markets = _FEE_RATE_FETCH(url)
        row = markets[0] if isinstance(markets, list) and markets else {}
        sched = row.get("feeSchedule") if isinstance(row.get("feeSchedule"), dict) else {}
        parsed = to_float(sched.get("rate"))
        if parsed is not None and parsed >= 0:
            rate, source = parsed, "feeSchedule"
        elif "feesEnabled" in row and not row.get("feesEnabled"):
            rate, source = 0.0, "fees_disabled"
    except Exception:
        return mgate.FEE_RATE_FALLBACK, "fallback"
    _FEE_RATE_CACHE[mid] = (rate, source, time.time())
    return rate, source


def _book_levels(candidate: dict, key: str) -> list:
    """Normalise whatever shape the book levels arrived in to [(price, size), ...]."""
    out: list = []
    for lv in (candidate.get(key) or []):
        try:
            if isinstance(lv, dict):
                p, s = float(lv.get("price")), float(lv.get("size"))
            else:
                p, s = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if p > 0 and s > 0:
            out.append((p, s))
    return sorted(out, key=lambda x: x[0], reverse=(key == "bid_levels"))


def _queue_aware_price(candidate: dict, *, ceiling: float, tick: float) -> dict:
    """Thin adapter onto the fill model's pricing rule. The rule itself lives in
    `polymarket_fill_model` next to the `queue_ahead` measurement it is derived
    from — one source of truth, so a change to the model cannot silently leave a
    stale copy of the rule behind in the execution path."""
    bids, asks = _book_levels(candidate, "bid_levels"), _book_levels(candidate, "ask_levels")
    if not asks:
        return {"ok": False, "reason": "queue-aware pricing needs full book depth"}
    try:
        from marketflow.execution import fill_model as fillmodel
        out = dict(fillmodel.queue_aware_maker_price(bids, asks, max_price=ceiling, tick=tick))
    except Exception as exc:  # noqa: BLE001 - pricing must never break a tick
        return {"ok": False, "reason": f"queue-aware pricing unavailable: {exc}"}
    if out.get("ok"):
        out["tick_size"] = round(tick, 8)
        out["executable_price"] = round(ceiling, 8)
    return out


def maker_limit_price(candidate: dict, *, executable_price: Optional[float],
                      queue_aware: bool = False) -> dict:
    """Choose a post-only BUY price inside the spread without crossing the ask.

    Two rules live here. The legacy one bids `best_bid + tick`: the CHEAPEST price
    that still counts as a maker order. The queue-aware one (fill model,
    `polymarket_fill_model.queue_aware_maker_price`) instead takes the MOST
    fillable price that still clears the edge ceiling. They differ whenever the
    spread is wider than two ticks, and the measured fill curve says the gap is
    large: every empty level inside the spread has the same queue_ahead of zero,
    so queue position cannot separate them, and what does separate them is
    distance from mid — where the legacy rule deliberately picks the far end
    (measured 1h fill 4.7% at 5c out vs 27.5% at 0.5c).

    `queue_aware` is off by default because the price of a live BUY is a
    money-surface behaviour change; it is enabled per-deployment via the daemon
    config key `maker_pricing: "queue_aware"`. Enabling it never loosens a fuse:
    the chosen price is still bounded by the caller's edge ceiling, still strictly
    below best_ask, and every downstream gate (Kelly, caps, S1) is untouched.
    """
    ask = to_float(executable_price)
    if ask is None or ask <= 0:
        return {"ok": False, "reason": "missing executable ask/max price for maker price"}
    tick = to_float(candidate.get("tick_size")) or DEFAULT_ENTRY_TICK_SIZE
    best_bid = to_float(candidate.get("best_bid") or candidate.get("bid_price"))
    if queue_aware:
        # Ceiling = what the caller already proved is +EV. `executable_price` is the
        # price the edge test cleared, so paying up to it keeps the trade +EV; paying
        # past it does not, no matter how much easier it would fill.
        qa = _queue_aware_price(candidate, ceiling=ask, tick=tick)
        if qa.get("ok"):
            return qa
        # Fail-soft: an unusable book must never stop an authorised entry. Fall
        # through to the legacy rule and record why.
    if best_bid is not None and best_bid > 0 and best_bid + tick < ask:
        price = best_bid + tick
        basis = "best_bid_plus_tick"
    else:
        price = ask - tick
        basis = "ask_minus_tick"
    price = max(pmx.MIN_LIMIT_PRICE, min(pmx.MAX_LIMIT_PRICE, price))
    if price >= ask:
        price = max(pmx.MIN_LIMIT_PRICE, ask - tick)
    return {
        "ok": price > 0 and price < 1,
        "basis": basis,
        "price": round(price, 8),
        "tick_size": round(tick, 8),
        "best_bid": round(best_bid, 8) if best_bid is not None else None,
        "executable_price": round(ask, 8),
    }


def fractional_kelly_buy_sizing(
    candidate: dict,
    *,
    model_probability: Optional[float],
    executable_price: Optional[float],
    order_price: Optional[float],
    cap_guard: "CapGuard",
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    fee_bps: float = 0.0,
) -> dict:
    """Fractional-Kelly sizing that preserves the exchange-minimum start size.

    The Kelly FRACTION comes from the chassis (`possizer._side_target_fraction`):
    a conservative-quantile p_c = p - z*sigma_p (sized down by the candidate's belief
    uncertainty when present) and the verified per-share fee. The EV gate, caps,
    exchange-minimum floor and S1 fuses are unchanged. With no uncertainty info and a
    maker open the fraction is identical to the prior raw-edge Kelly (no regression)."""
    p = to_float(model_probability)
    market_px = to_float(executable_price)
    px = to_float(order_price)
    if p is None or market_px is None or px is None or not (0 < p < 1) or not (0 < market_px < 1) or px <= 0:
        return {"ok": False, "reason": "missing probability or price for Kelly sizing"}
    min_size = minimum_buy_sizing(candidate, entry_price=px, max_price=px)
    if not min_size.get("ok"):
        return min_size
    # Conservative-quantile Kelly fraction via the chassis. cost_buffer/edge_buffer 0:
    # the EV gate (entry_should_buy, 5% edge_buffer + fee) already gated +EV upstream;
    # this is only the sizing fraction. residual_lambda 1 => no market double-shrink.
    q_mid = to_float(candidate.get("market_prob"))
    div = to_float(candidate.get("divergence"))
    cred = to_float(candidate.get("credibility"))
    p_sd = possizer.prob_uncertainty(div, cred) if (div is not None or cred is not None) else 0.0
    maker = not is_speed_window_candidate(candidate)
    size_cfg = possizer.SizerConfig(kelly_fraction=max(0.0, min(1.0, kelly_fraction)),
                                    cost_buffer=0.0, edge_buffer=0.0, residual_lambda=1.0)
    fractional, _p_eff, p_c, ask_eff = possizer._side_target_fraction(
        p, p_sd, market_px, q_mid, size_cfg, fee_rate=max(0.0, fee_bps) / 10000.0, maker=maker)
    edge = round((p_c if p_c is not None else p) - (ask_eff if ask_eff is not None else market_px), 8)
    if fractional is None or fractional <= 0:
        return {"ok": False, "reason": "conservative-Kelly fraction non-positive after uncertainty/fee", "edge": edge}
    full_kelly_fraction = fractional / max(1e-9, max(0.0, min(1.0, kelly_fraction)))
    # The Kelly bankroll is the account's real capital, not the cap total. Treating
    # the cap as the bankroll makes the target identically "cap total * Kelly
    # fraction", so every order hits the per-trade cap — and when real capital is far
    # below the cap total that is close to going all in every time. A cap is a fuse,
    # not a stake. Priority: explicit on the intent > the real balance refreshed each
    # tick > the cap total, which is the fail-soft when the balance cannot be read,
    # with the venue's own balance check as backstop.
    bankroll = to_float(candidate.get("kelly_bankroll_usd"))
    if bankroll is None or bankroll <= 0:
        live_balance = to_float(getattr(cap_guard, "available_collateral_usd", None))
        bankroll = live_balance if (live_balance is not None and live_balance > 0) else cap_guard.total_cap_usd
    cap_remaining = max(0.0, cap_guard.total_cap_usd - cap_guard.deployed_usd)
    spend_ceiling = min(cap_guard.per_trade_cap_usd, cap_remaining)
    # Real balance is a THIRD ceiling next to per-trade and remaining cap (260725).
    # Without it every order is built at the cap and the exchange rejects it for
    # insufficient balance — the whole entry path then spins on a gap of a few cents.
    # 2% headroom absorbs price drift between sizing and fill. Unknown balance keeps
    # the previous cap-only behaviour (the exchange remains the backstop).
    balance = to_float(getattr(cap_guard, "available_collateral_usd", None))
    if balance is not None and balance >= 0:
        spend_ceiling = min(spend_ceiling, balance * 0.98)
    min_notional = float(min_size["estimated_notional_usd"])
    target_notional = bankroll * fractional
    if spend_ceiling + 1e-9 < min_notional:
        return {
            "ok": False,
            "reason": "exchange minimum exceeds remaining/per-trade cap",
            "sizing_policy": "fractional_kelly_exchange_minimum_blocked",
            "minimum_notional_usd": round(min_notional, 8),
            "spend_ceiling_usd": round(spend_ceiling, 8),
        }
    final_notional = min(spend_ceiling, max(min_notional, target_notional))
    uplift = target_notional < min_notional
    # The exchange minimum must not take over. Rounding a sub-minimum Kelly target
    # up is allowed only while the minimum is still a small share of the bankroll;
    # otherwise a small account is pushed into overexposure by the venue's own floor
    # — the same error in a different guise.
    if uplift and min_notional > bankroll * 0.25:
        return {
            "ok": False,
            "reason": "exchange minimum would exceed 25% of real bankroll",
            "sizing_policy": "fractional_kelly_exchange_minimum_blocked",
            "minimum_notional_usd": round(min_notional, 8),
            "kelly_bankroll_usd": round(bankroll, 8),
        }
    shares = max(float(min_size["order_min_size"]), final_notional / px)
    final_notional = shares * px
    return {
        "ok": True,
        "sizing_policy": "fractional_kelly_with_exchange_minimum",
        "model_probability": round(p, 8),
        "executable_price": round(market_px, 8),
        "order_price": round(px, 8),
        "edge": round(edge, 8),
        "full_kelly_fraction": round(full_kelly_fraction, 8),
        "kelly_fraction": round(max(0.0, min(1.0, kelly_fraction)), 8),
        "fractional_kelly_fraction": round(fractional, 8),
        "kelly_bankroll_usd": round(bankroll, 8),
        "kelly_target_notional_usd": round(target_notional, 8),
        "exchange_minimum_lifted": uplift,
        "order_min_size": min_size["order_min_size"],
        "shares": round(shares, 8),
        "estimated_notional_usd": round(final_notional, 8),
        "max_spend_usd": round(final_notional, 8),
        "spend_ceiling_usd": round(spend_ceiling, 8),
    }


def is_speed_window_candidate(candidate: dict) -> bool:
    return (
        monitor.safe_str(candidate.get("taker_allowed_reason")) == SPEED_WINDOW_TAKER_REASON
        or bool(candidate.get("speed_window"))
        or bool(candidate.get("speed_window_candidate"))
    )


@dataclass
class WinRateResult:
    win_probability: Optional[float]
    source: str
    as_of: str
    confidence: str
    model_version: Optional[str] = None
    source_class: str = "MODEL_PROBABILITY"
    reject_reason_codes: list[str] = field(default_factory=list)
    notes: str = ""
    event_detection: dict = field(default_factory=dict)


class WinRateProvider:
    """S2 seam: returns the true held-side payout probability for a position.

    Implementations: a sports/win-rate model (API-Football / Sportmonks / Opta)
    or an MarketFlow engine probability provider. The daemon consumes whatever this
    returns; it never invents a probability itself.
    """

    name = "base"

    def get_win_probability(self, position_ctx: dict) -> WinRateResult:  # pragma: no cover - abstract
        raise NotImplementedError


class StubWinRateProvider(WinRateProvider):
    """Bridges the monitor's existing probability_override contract.

    Until S2 is merged, the only honest win-rate source is an operator-supplied
    probability_override (interpreted as the held side's true payout probability).
    Absent that, win_probability is None -> the position resolves to OBSERVE_ONLY,
    never a fabricated number.
    """

    name = "stub_probability_override"

    def get_win_probability(self, position_ctx: dict) -> WinRateResult:
        raw = position_ctx.get("probability_override")
        p = monitor.clamp_prob(raw)
        if p is None:
            return WinRateResult(
                win_probability=None,
                source="stub_none",
                as_of=iso_now(),
                confidence="stub",
                source_class="NO_MODEL_PROBABILITY",
                reject_reason_codes=["MISSING_P_YES_MODEL"],
                notes="no S2 win-rate provider merged and no probability_override supplied",
            )
        return WinRateResult(
            win_probability=p,
            source="stub_probability_override",
            as_of=iso_now(),
            confidence="stub",
            model_version="probability-override",
            notes="probability_override used as held-side payout probability (S2 not merged)",
        )


@dataclass
class OrderIntent:
    intent_id: str
    action: str
    is_exit: bool
    market_id: Optional[str]
    market_slug: Optional[str]
    token_id: Optional[str]
    side: Optional[str]
    shares: float
    limit_price: Optional[float]
    est_notional_usd: Optional[float]
    max_loss_usd: Optional[float]
    reason: str
    order_kind: str = "limit"
    market_order_type: str = "FOK"
    post_only: bool = True
    taker_allowed_reason: Optional[str] = None
    max_spend_usd: Optional[float] = None
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    held_shares: Optional[float] = None
    idempotency_key: Optional[str] = None
    settlement_cycle: Optional[str] = None
    price_floor_override_reason: Optional[str] = None  # carried to S1's BUY price floor


@dataclass
class ExecutionResult:
    accepted: bool
    executed: bool
    effective_mode: str
    simulated: bool
    order_id: Optional[str]
    reason: str
    intent: dict = field(default_factory=dict)


class ExecutionAdapter:
    """S1 seam: turns an OrderIntent into a real CLOB place/cancel call.

    The real S1 adapter owns SDK signing, signature_type/funder wiring, and the
    AUTHORITATIVE capital fuse. The daemon only decides what to attempt and in
    which mode; S1 is the last line that can refuse.
    """

    name = "base"

    def execute(self, intent: OrderIntent, *, effective_mode: str) -> ExecutionResult:  # pragma: no cover - abstract
        raise NotImplementedError


class DryRunStubExecutionAdapter(ExecutionAdapter):
    """Executes NOTHING. Logs the intent and reports a simulated outcome.

    In dry_run this is the normal no-op. In live mode it is a fail-safe: until
    the S1 execution stack is selected (--executor s1), no order is ever placed,
    even with an armed arm-state. This keeps the daemon safe to run 24/7.
    """

    name = "dry_run_stub"

    def execute(self, intent: OrderIntent, *, effective_mode: str) -> ExecutionResult:
        if effective_mode == "live":
            return ExecutionResult(
                accepted=False,
                executed=False,
                effective_mode=effective_mode,
                simulated=False,
                order_id=None,
                reason="live requested but S1 execution adapter not merged; no order placed (fail-safe)",
                intent=asdict(intent),
            )
        return ExecutionResult(
            accepted=True,
            executed=False,
            effective_mode=effective_mode,
            simulated=True,
            order_id=None,
            reason="dry_run stub: intent recorded, no live order placed",
            intent=asdict(intent),
        )


class S1ExecutionAdapter(ExecutionAdapter):
    """S1 wiring: routes intents through polymarket_execution's canonical flow.

    Uses S1's own plan_order -> (execute_order when will_execute_live) path so S1's
    fuses (arm-state + kill + caps) stay authoritative. The daemon LiveGate only
    decides whether to REQUEST live; S1 decides whether to ACTUALLY execute. Live
    executed records are appended to S1's own ledger so its cumulative deploy cap
    stays correct (only LIVE_EXECUTED BUY rows count there).
    """

    name = "s1_polymarket_execution"

    def __init__(self, config: Optional[dict] = None, *, alerter: Optional[Alerter] = None):
        self._cfg = config or {}
        self._alerter = alerter

    def _resolve(self, pmx: Any) -> dict:
        cfg = self._cfg
        return {
            "kill_file": cfg.get("kill_file", pmx.DEFAULT_KILL_FILE),
            "arm_state_file": cfg.get("arm_state_file", pmx.DEFAULT_ARM_STATE_FILE),
            "secret_dir": cfg.get("secret_dir", pmx.DEFAULT_SECRET_DIR),
            "ledger_path": cfg.get("ledger_path", pmx.DEFAULT_LEDGER),
            "expected_wallet_type": cfg.get("expected_wallet_type", pmx.EXPECTED_WALLET_TYPE),
            "estimate_fill": bool(cfg.get("estimate_fill", False)),
            "exit_order_kind": str(cfg.get("exit_order_kind", "limit")),
            "max_total_deploy_usd": float(cfg.get("max_total_deploy_usd", pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD)),
            "max_per_trade_usd": float(cfg.get("max_per_trade_usd", pmx.DEFAULT_MAX_PER_TRADE_USD)),
        }

    def _s1_intent(self, pmx: Any, intent: OrderIntent, d: dict) -> Any:
        if intent.is_exit:
            kind = d["exit_order_kind"] if intent.limit_price is not None else "market"
            return pmx.build_exit_intent(
                token_id=intent.token_id,
                held_shares=intent.held_shares,
                order_kind=kind,
                price=intent.limit_price,
                size=intent.shares,
                min_price=intent.min_price if intent.min_price is not None else intent.limit_price,
                market_id=intent.market_id,
                market_slug=intent.market_slug,
                outcome=intent.side,
                signal_id=intent.intent_id,
                idempotency_key=intent.intent_id,
                note=intent.reason,
                settlement_cycle=intent.settlement_cycle,
            )
        return pmx.build_entry_intent(
            token_id=intent.token_id,
            order_kind=intent.order_kind,
            price=intent.limit_price,
            size=intent.shares,
            max_spend_usd=intent.max_spend_usd,
            max_price=intent.max_price,
            market_order_type=intent.market_order_type,
            post_only=intent.post_only,
            taker_allowed_reason=intent.taker_allowed_reason,
            market_id=intent.market_id,
            market_slug=intent.market_slug,
            outcome=intent.side,
            signal_id=intent.idempotency_key or intent.intent_id,
            idempotency_key=intent.idempotency_key or intent.intent_id,
            note=intent.reason,
            settlement_cycle=intent.settlement_cycle,
            price_floor_override_reason=intent.price_floor_override_reason,
        )

    def execute(self, intent: OrderIntent, *, effective_mode: str) -> ExecutionResult:
        try:
            from marketflow.execution import orders as pmx
        except Exception as exc:
            return ExecutionResult(False, False, effective_mode, False, None,
                                   f"S1 module unavailable: {exc}", asdict(intent))
        try:
            d = self._resolve(pmx)
            s1_intent = self._s1_intent(pmx, intent, d)
            caps = pmx.owner_fuse_caps(max_total_deploy_usd=d["max_total_deploy_usd"], max_per_trade_usd=d["max_per_trade_usd"])
            book = None
            if d["estimate_fill"]:
                try:
                    book = pmx.fetch_book_readonly(s1_intent.token_id)
                except Exception:
                    book = None
            plan = pmx.plan_order(
                s1_intent,
                requested_live=(effective_mode == "live"),
                caps=caps,
                kill_file=d["kill_file"],
                arm_state_file=d["arm_state_file"],
                ledger_path=d["ledger_path"],
                book=book,
            )
            record = plan
            if plan.get("will_execute_live"):
                secrets = pmx.load_polymarket_secret_refs(d["secret_dir"])
                # Building a venue client performs several sequential authenticated
                # reads, which can be slow or half-dead through a tunnel. The httpx
                # layer is already hardened with timeouts; this adds a wall-clock
                # interrupt as defence in depth. A stalled build — read-only and
                # idempotent — raises, the tick fails cleanly, and the next one
                # retries rather than hanging forever. **It wraps the build only, never
                # order submission**: once an order is posted it must not be
                # interrupted, or idempotency breaks.
                client = _with_hard_timeout(
                    float(d.get("live_client_build_timeout_sec", 25.0)),
                    "live SecureClient build",
                    lambda: pmx.build_secure_client(
                        secrets, secret_dir=d["secret_dir"], side=s1_intent.side
                    ),
                )
                try:
                    record = pmx.execute_order(
                        client,
                        s1_intent,
                        plan,
                        expected_wallet_type=d["expected_wallet_type"],
                        ledger_path=d["ledger_path"],
                    )
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
                leaks = pmx.assert_no_secret_leak(record, secrets)
                if leaks:
                    payload = {
                        "leaks": sorted(leaks),
                        "ledger_path": d["ledger_path"],
                        "idempotency_key": record.get("idempotency_key"),
                        "execution_id": record.get("execution_id"),
                    }
                    try:
                        pmx.engage_global_halt("S1 secret leak guard tripped after live order submission")
                        payload["halt_engaged"] = True
                    except Exception as halt_exc:
                        payload["halt_engaged"] = False
                        payload["halt_error"] = str(halt_exc)
                    record["secret_leak_guard"] = payload
                    if self._alerter is not None:
                        self._alerter.alert("s1_secret_leak_guard_tripped", payload)
        except Exception as exc:
            failure_intent = {"daemon": asdict(intent)}
            return ExecutionResult(False, False, effective_mode, False, None,
                                   f"S1 execute failed: {exc}", failure_intent)
        will_live = bool(plan.get("will_execute_live"))
        fill = record.get("fill") or {}
        receipt = record.get("receipt") or {}
        order_id = fill.get("order_id") or receipt.get("orderID") or receipt.get("order_id")
        result_intent = {"daemon": asdict(intent), "s1": record}
        return ExecutionResult(
            accepted=bool(plan.get("would_place")),
            executed=bool(record.get("executed", False)),
            effective_mode=effective_mode,
            simulated=not will_live,
            order_id=monitor.safe_str(order_id),
            reason=f"S1 {record.get('mode')}; would_place={plan.get('would_place')}; refusals={plan.get('fuses', {}).get('refusals')}",
            intent=result_intent,
        )


class LiveGate:
    """Resolves the effective execution mode from the unified arm-state.

    Single source of truth: it reads the same arm-state file S1 reads (via
    pmx.load_arm_state). Live requires --live AND an armed, bridge-written,
    non-expired, un-tampered arm-state whose mode is exit_only or full. The arm
    mode also tells the daemon whether autonomous ENTRY runs (full) or only
    EXIT (exit_only). S1's plan_order independently re-checks the same file per
    order and stays the authoritative fuse; this gate is the daemon-loop view.
    """

    def __init__(self, requested_live: bool, arm_state_file: str):
        self.requested_live = bool(requested_live)
        self.arm_state_file = arm_state_file

    def resolve(self) -> dict:
        try:
            # Owner path: read the owner's own arm-state with the owner ceiling so her
            # bridge-set caps (up to the fat-finger ceiling) are honored as the
            # effective caps instead of tamper. A missing cap field falls back to
            # the default weld ($25/$5). External multi-tenant users run a SEPARATE
            # process with default-welded FuseCaps.
            arm = pmx.load_arm_state(self.arm_state_file, pmx.owner_fuse_caps())
        except Exception as exc:  # fail-safe: any read error => dry_run
            return {
                "effective_mode": "dry_run",
                "requested_live": self.requested_live,
                "armed": False,
                "arm_mode": "off",
                "arm_state_file": self.arm_state_file,
                "reason": f"arm-state read failed ({exc}); staying dry_run",
            }
        armed = bool(arm.get("valid"))
        arm_mode = arm.get("mode", "off")
        if self.requested_live and armed:
            mode, reason = "live", f"live requested and arm-state armed (mode={arm_mode})"
        elif self.requested_live and not armed:
            mode, reason = "dry_run", f"live requested but arm-state not armed ({arm.get('reason')})"
        else:
            mode, reason = "dry_run", "live not requested; default dry_run"
        return {
            "effective_mode": mode,
            "requested_live": self.requested_live,
            "armed": armed,
            "arm_mode": arm_mode,
            "arm_state_file": self.arm_state_file,
            "reason": reason,
            "effective_max_total_deploy_usd": arm.get("effective_max_total_deploy_usd"),
            "effective_max_per_trade_usd": arm.get("effective_max_per_trade_usd"),
            "budget_epoch": arm.get("budget_epoch"),
        }


class KillSwitch:
    """File-based plus signal-based emergency halt."""

    def __init__(self, kill_file: str):
        self.kill_file = kill_file
        self._signalled = False
        self._signal_name: Optional[str] = None

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self._signalled = True
        try:
            self._signal_name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - non-standard signum
            self._signal_name = str(signum)

    def file_active(self) -> bool:
        return bool(self.kill_file) and os.path.exists(self.kill_file)

    def global_halt_active(self) -> bool:
        return os.path.exists(pmx.DEFAULT_GLOBAL_HALT_FILE)

    def signalled(self) -> bool:
        return self._signalled

    def active(self) -> bool:
        return self.file_active() or self.signalled()

    def buy_halted(self) -> bool:
        return self.active() or self.global_halt_active()

    def status(self) -> dict:
        return {
            "kill_file": self.kill_file,
            "file_active": self.file_active(),
            "global_halt_file": pmx.DEFAULT_GLOBAL_HALT_FILE,
            "global_halt_active": self.global_halt_active(),
            "signalled": self._signalled,
            "signal_name": self._signal_name,
            "active": self.active(),
            "buy_halted": self.buy_halted(),
        }


class SingleInstanceLock:
    """Advisory pid lock for one daemon process per workspace."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        ensure_parent(self.path)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DaemonError(f"another polymarket daemon instance already holds {self.path}") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()} {iso_now()}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        finally:
            self._fh = None


class CapGuard:
    """Defense-in-depth capital fuse. S1 is authoritative; this pre-checks entries.

    Exits reduce exposure and are never blocked here. Only entries consume budget.
    """

    def __init__(self, total_cap_usd: float, per_trade_cap_usd: float):
        self.total_cap_usd = float(total_cap_usd)
        self.per_trade_cap_usd = float(per_trade_cap_usd)
        self.deployed_usd = 0.0
        self.last_budget_epoch: Optional[str] = None
        # Real spendable collateral on the account, refreshed per tick from the SDK
        # (260725). None = unknown -> sizing keeps its cap-only behaviour and the
        # exchange stays the backstop. Caps say how much we're ALLOWED to deploy;
        # this says how much we actually HAVE. Sizing needs both, or every order is
        # built at the cap and rejected by the exchange for insufficient balance.
        self.available_collateral_usd: Optional[float] = None

    def check_entry(self, notional_usd: Optional[float]) -> dict:
        n = to_float(notional_usd)
        if n is None or n <= 0:
            return {"allowed": False, "reason": "entry notional must be a positive number", "advisory": True}
        if n > self.per_trade_cap_usd + 1e-9:
            return {
                "allowed": False,
                "reason": "entry exceeds per-trade cap (S1 is authoritative)",
                "advisory": True,
                "per_trade_cap_usd": self.per_trade_cap_usd,
            }
        if self.deployed_usd + n > self.total_cap_usd + 1e-9:
            return {
                "allowed": False,
                "reason": "entry would exceed total deployment cap (S1 is authoritative)",
                "advisory": True,
                "total_cap_usd": self.total_cap_usd,
                "deployed_usd": round(self.deployed_usd, 8),
            }
        return {
            "allowed": True,
            "reason": "within advisory caps; S1 makes the final call",
            "advisory": True,
            "remaining_usd": round(self.total_cap_usd - self.deployed_usd, 8),
        }

    def record_entry(self, notional_usd: Optional[float]) -> None:
        n = to_float(notional_usd)
        if n and n > 0:
            self.deployed_usd += n

    def sync_deployed_floor(self, deployed_usd: Optional[float]) -> None:
        n = to_float(deployed_usd)
        if n is not None and n > self.deployed_usd:
            self.deployed_usd = n

    def reset_tick_window(self) -> None:
        """net-exposure semantics: advisory deployed counts only THIS tick's
        entries (anti burst-buy). Cross-tick budget truth lives in S1's account
        rebuild (net-exposure cap + drawdown fuse, fail-closed), re-checked
        before every live BUY. The old cross-tick accumulation double-counted
        settled fills and blocked compounding."""
        self.deployed_usd = 0.0

    def sync_epoch(self, budget_epoch: Optional[str]) -> bool:
        """A budget-epoch change (an explicit reopen upstream) resets the local
        counter so it agrees with the execution layer.

        The authoritative gate rebuilds the budget from the account's real fills
        starting at the epoch. This guard is an in-process advisory pre-check, and
        without following the epoch it would hold the previous epoch's deployment
        against the new one forever. The first epoch seen after start is recorded
        but does not reset; a change after that resets deployed, with the
        authoritative gate still checking the first order from account state.
        """
        epoch = (budget_epoch or "").strip() or None
        if epoch is None or epoch == self.last_budget_epoch:
            return False
        first_seen = self.last_budget_epoch is None
        self.last_budget_epoch = epoch
        if first_seen:
            return False
        self.deployed_usd = 0.0
        return True

    def status(self) -> dict:
        return {
            "total_cap_usd": self.total_cap_usd,
            "per_trade_cap_usd": self.per_trade_cap_usd,
            "deployed_usd": round(self.deployed_usd, 8),
            "remaining_usd": round(self.total_cap_usd - self.deployed_usd, 8),
            "authoritative_fuse": "S1_execution_adapter",
        }


class Alerter:
    """Optional file alerter. No network, no secrets. Disabled by default."""

    def __init__(self, enabled: bool, alert_file: str):
        self.enabled = bool(enabled)
        self.alert_file = alert_file

    def alert(self, kind: str, payload: dict) -> None:
        if not self.enabled:
            return
        row = {"generated_at": iso_now(), "kind": kind, "payload": payload}
        try:
            append_jsonl_rotating(self.alert_file, row)
        except OSError:
            pass


class AutonomousTurnClient:
    """Transport client for engine-native autonomous turns."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.bridge_url = validated_local_bridge_url(cfg.get("bridge_url"))
        self.timeout_sec = max(5.0, float(cfg.get("timeout_sec", 180.0)))
        self.poll_sec = max(0.25, float(cfg.get("poll_sec", 2.0)))
        self.objective = str(cfg.get("objective") or "")
        self.trigger_on_tick = bool(cfg.get("trigger_on_tick", False))
        self.min_interval_sec = max(0.0, float(cfg.get("min_interval_sec", 900.0)))
        self.dry_run_only = cfg.get("dry_run_only") is not False
        self.ledger_path = str(cfg.get("ledger_path") or trade_experience.DEFAULT_LEDGER)
        self.mock_decision = cfg.get("mock_decision") if isinstance(cfg.get("mock_decision"), dict) else None
        self._last_turn_ts = 0.0

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "trigger_on_tick": self.trigger_on_tick,
            "min_interval_sec": self.min_interval_sec,
        }

    def should_trigger(self, record: dict) -> tuple[bool, str]:
        if not self.enabled:
            return False, "autonomous turn disabled"
        if record.get("halt"):
            return False, "halted"
        for rec in record.get("positions") or []:
            if rec.get("decision") in (
                "SELL_SIGNAL",
                "CLAIM_REDEEM_SIGNAL",
                "OBSERVE_ONLY",
                "LIQUIDITY_BLOCKED",
                "MARKET_DATA_BLOCKED",
            ):
                return False, f"exit handled mechanically:{rec.get('decision')}"
        if self._last_turn_ts and (time.time() - self._last_turn_ts) < self.min_interval_sec:
            remaining = int(self.min_interval_sec - (time.time() - self._last_turn_ts))
            return False, f"autonomous turn cooling down ({remaining}s)"
        if self.trigger_on_tick:
            return True, "scheduled_check"
        return False, "scheduled autonomous turn disabled"

    def run(self, *, record: dict, trigger_reason: str) -> dict:
        turn_id = f"auto_{now_ms()}_{hash_obj({'tick': record.get('tick_id'), 'trigger': trigger_reason})[:10]}"
        turn_ts = time.time()
        self._last_turn_ts = turn_ts
        if self.mock_decision is not None:
            row = dict(self.mock_decision)
            row["turn_id"] = turn_id
            row.setdefault("source", "daemon_selftest_mock")
            decision = trade_experience.append_experience(row, ledger_path=self.ledger_path)
            return {
                "enabled": True,
                "transport": "mock",
                "turn_id": turn_id,
                "status": "completed",
                "trigger_reason": trigger_reason,
                "dry_run_only": self.dry_run_only,
                "internal change log": decision,
            }

        payload = {
            "turn_id": turn_id,
            "objective": self.objective,
            "trigger_reason": trigger_reason,
            "positions": record.get("positions", []),
            "market_state": {
                "live_gate": record.get("live_gate"),
                "caps": record.get("caps"),
                "executor": record.get("executor"),
                "win_rate_provider": record.get("win_rate_provider"),
                "autonomous_dry_run_only": self.dry_run_only,
            },
            "daemon": {
                "tick_id": record.get("tick_id"),
                "generated_at": record.get("generated_at"),
                "schema_version": record.get("schema_version"),
            },
        }
        try:
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            req = urllib.request.Request(
                self.bridge_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=min(self.timeout_sec, 30.0)) as resp:
                accepted = json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            return {
                "enabled": True,
                "transport": "bridge",
                "turn_id": turn_id,
                "status": "error",
                "trigger_reason": trigger_reason,
                "error": str(exc),
            }

        client_msg_id = str(accepted.get("client_msg_id") or "")
        status_url = self._status_url(client_msg_id)
        status_payload: dict[str, Any] = {"status": accepted.get("status")}
        deadline = time.time() + self.timeout_sec
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(status_url, timeout=5.0) as resp:
                    status_payload = json.loads(resp.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                status_payload = {"status": "error", "error": str(exc)}
                break
            if status_payload.get("status") not in ("queued", "running", "cancelling"):
                break
            time.sleep(self.poll_sec)
        decision = trade_experience.latest_for_turn(turn_id, ledger_path=self.ledger_path)
        return {
            "enabled": True,
            "transport": "bridge",
            "turn_id": turn_id,
            "status": status_payload.get("status"),
            "trigger_reason": trigger_reason,
            "dry_run_only": self.dry_run_only,
            "client_msg_id": client_msg_id,
            "chat_session_id": accepted.get("chat_session_id"),
            "internal change log": decision,
            "error": status_payload.get("error") or accepted.get("error"),
        }

    def _status_url(self, client_msg_id: str) -> str:
        base = self.bridge_url.split("/api/chat/autonomous", 1)[0]
        return f"{base}/api/chat/status?client_msg_id={urllib.parse.quote(client_msg_id)}"


def critical_win_rate_for_buy(ask_price: float, edge_buffer: float, fee_bps: float,
                              *, fee_per_share: Optional[float] = None) -> float:
    """Break-even true probability for a BUY at `ask_price`.

    `fee_per_share` is the verified per-share fee for THIS order (0 for a maker
    open, `rate·px·(1−px)` for a taker) and supersedes the flat `fee_bps` when
    given — same units as the price, so it adds directly. Flat bps stays the
    fallback for callers that have no per-market rate."""
    fee = to_float(fee_per_share)
    if fee is None:
        fee = max(0.0, fee_bps) / 10000.0
    return min(1.0, ask_price + max(0.0, edge_buffer) + max(0.0, fee))


def entry_should_buy(true_p: Optional[float], ask_price: Optional[float], edge_buffer: float, fee_bps: float,
                     *, fee_per_share: Optional[float] = None) -> bool:
    tp = to_float(true_p)
    ap = to_float(ask_price)
    if tp is None or ap is None or ap <= 0 or ap >= 1:
        return False
    return tp > critical_win_rate_for_buy(ap, edge_buffer, fee_bps, fee_per_share=fee_per_share)


def market_quality_policy(config: Optional[dict] = None) -> dict:
    raw = dict(DEFAULT_MARKET_QUALITY)
    if isinstance(config, dict):
        raw.update(config)
    out = dict(DEFAULT_MARKET_QUALITY)
    for key in (
        "longshot_low",
        "longshot_high",
        "auto_band_low",
        "auto_band_high",
        "min_edge_after_cost",
        "cost_buffer",
    ):
        value = to_float(raw.get(key))
        if value is not None:
            out[key] = value
    for key in ("require_model_probability", "require_resolution", "edge_advisory"):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            out[key] = bool(value)
    return out


def resolution_attestation_for_entry(candidate: dict) -> dict[str, Any]:
    raw = candidate.get("resolution_confirmed_clean")
    raw_key = "resolution_confirmed_clean"
    if raw is None and "clean_resolution" in candidate:
        raw = candidate.get("clean_resolution")
        raw_key = "clean_resolution"
    source = monitor.safe_str(candidate.get("source"))
    candidate_type = monitor.safe_str(candidate.get("candidate_type"))
    attestation_source = monitor.safe_str(candidate.get("resolution_attestation_source"))
    resolution_source = monitor.safe_str(candidate.get("resolution_source"))
    raw_true = raw is True
    self_attested = raw_true and source in SELF_ATTESTED_RESOLUTION_SOURCES
    independent = raw_true and (
        attestation_source is not None
        or raw_key == "clean_resolution"
        or candidate_type == "supervised_canary"
    )
    confirmed = bool(self_attested or independent)
    if self_attested:
        trust = "self_attested_llm"
    elif independent:
        trust = attestation_source or "candidate_clean_resolution"
    elif raw is None:
        trust = "missing"
    else:
        trust = "untrusted_or_false"
    return {
        "confirmed_clean_for_gate": confirmed,
        "raw_key": raw_key,
        "raw_value": raw,
        "source": source,
        "candidate_type": candidate_type,
        "resolution_source": resolution_source,
        "attestation_source": attestation_source,
        "trust": trust,
        "self_attested": self_attested,
    }


def discovered_position_for_token(positions: Any, token_id: str) -> dict[str, Any]:
    token = monitor.safe_str(token_id)
    if not token or not isinstance(positions, list):
        return {
            "ok": False,
            "reason": "current discovered positions unavailable",
            "token_id": token,
            "positions_available": isinstance(positions, list),
        }
    held = 0.0
    matched = 0
    entry_cost_total = 0.0
    entry_cost_complete = True
    markets: list[str] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        summary = position.get("_private_position_summary")
        if not isinstance(summary, dict):
            summary = {}
        pos_token = monitor.safe_str(position.get("token_id") or summary.get("token_id"))
        if pos_token != token:
            continue
        matched += 1
        shares = to_float(position.get("shares") or summary.get("size"))
        if shares is None or shares <= 0:
            return {
                "ok": False,
                "reason": "matching position has no positive shares",
                "token_id": token,
                "matched_positions": matched,
            }
        held += shares
        entry_cost = to_float(position.get("entry_cost") or summary.get("initial_value"))
        if entry_cost is None:
            entry_cost_complete = False
        else:
            entry_cost_total += entry_cost
        market_slug = monitor.safe_str(position.get("market_slug") or summary.get("slug"))
        if market_slug:
            markets.append(market_slug)
    if matched <= 0 or held <= 0:
        return {
            "ok": False,
            "reason": "no matching live position for token_id",
            "token_id": token,
            "positions_count": len(positions),
        }
    return {
        "ok": True,
        "token_id": token,
        "held_shares": round(held, 8),
        "matched_positions": matched,
        "entry_cost_usd": round(entry_cost_total, 8) if entry_cost_complete else None,
        "entry_cost_complete": entry_cost_complete,
        "market_slugs": sorted(set(markets)),
    }


def proportional_exit_max_loss(*, shares: float, held_shares: float, min_price: float, position_lookup: dict[str, Any]) -> float | None:
    entry_cost = to_float(position_lookup.get("entry_cost_usd"))
    if entry_cost is None or held_shares <= 0:
        return None
    proceeds = shares * min_price
    proportional_cost = entry_cost * (shares / held_shares)
    return round(max(0.0, proportional_cost - proceeds), 8)


def _position_peak_key(position_config: dict) -> Optional[str]:
    """Stable per-position key for the sell-price high-water-mark."""
    mid = position_config.get("market_id") or position_config.get("condition_id") or position_config.get("conditionId")
    tid = position_config.get("token_id") or position_config.get("tokenId")
    if mid is None and tid is None:
        return None
    return f"{mid}:{tid}"


def build_exit_intent(position_result: dict, *, sell_shares: float | None = None) -> OrderIntent:
    market = position_result.get("market", {})
    token = position_result.get("token", {})
    pos = position_result.get("position", {})
    val = position_result.get("valuation", {})
    sweep = val.get("sweep", {})
    held = to_float(pos.get("shares")) or 0.0
    # Full exit by default; a TRIM books only `sell_shares` (clamped to held).
    is_partial = sell_shares is not None
    shares = held if not is_partial else max(0.0, min(float(sell_shares), held))
    limit_price = sweep.get("terminal_price")
    entry_cost = to_float(pos.get("entry_cost"))
    if not is_partial:
        est_proceeds = val.get("immediate_exit_value")
        max_loss = None
        if entry_cost is not None and est_proceeds is not None:
            max_loss = round(max(0.0, entry_cost - est_proceeds), 8)
        reason = "held-side true win-rate below immediate-sale break-even; exit to capture richer market price"
    else:
        # conservative: proceeds at the full-fill terminal price; loss vs the
        # proportional cost basis of just the trimmed shares.
        est_proceeds = round(shares * limit_price, 8) if limit_price is not None else None
        max_loss = None
        if entry_cost is not None and est_proceeds is not None and held > 0:
            proportional_cost = entry_cost * (shares / held)
            max_loss = round(max(0.0, proportional_cost - est_proceeds), 8)
        reason = "favourable swing booked: partial take-profit to a fractional-Kelly stake; +EV residual rides"
    raw_market = market.get("raw_compact") if isinstance(market.get("raw_compact"), dict) else {}
    intent_key = {
        "market_id": market.get("market_id"),
        "token_id": token.get("token_id"),
        "shares": shares,
        "action": "SELL",
    }
    return OrderIntent(
        intent_id=f"exit_{now_ms()}_{hash_obj(intent_key)[:10]}",
        action="SELL",
        is_exit=True,
        market_id=market.get("market_id"),
        market_slug=market.get("slug"),
        token_id=token.get("token_id"),
        side=pos.get("side"),
        shares=shares,
        held_shares=held,
        limit_price=limit_price,
        est_notional_usd=est_proceeds,
        max_loss_usd=max_loss,
        reason=reason,
        settlement_cycle=market.get("close_time") or raw_market.get("endDate"),
    )


def build_scale_in_intent(position_result: dict, *, add_shares: float, maker_price: float) -> OrderIntent:
    """A bounded maker BUY that ADDS to a held position when the fractional-Kelly
    target rose above current holding (favourable scale-in). Limit + post_only, so
    it never crosses/overpays; S1's plan_order re-checks arm-state, caps, budget,
    and kill before any real order — identical fuses to any other BUY."""
    market = position_result.get("market", {})
    token = position_result.get("token", {})
    pos = position_result.get("position", {})
    raw_market = market.get("raw_compact") if isinstance(market.get("raw_compact"), dict) else {}
    shares = max(0.0, float(add_shares))
    notional = round(shares * float(maker_price), 8)
    intent_key = {
        "market_id": market.get("market_id"),
        "token_id": token.get("token_id"),
        "shares": shares,
        "action": "BUY",
        "kind": "scale_in",
    }
    return OrderIntent(
        intent_id=f"scalein_{now_ms()}_{hash_obj(intent_key)[:10]}",
        action="BUY",
        is_exit=False,
        market_id=market.get("market_id"),
        market_slug=market.get("slug"),
        token_id=token.get("token_id"),
        side=pos.get("side"),
        shares=shares,
        limit_price=float(maker_price),
        order_kind="limit",
        post_only=True,
        est_notional_usd=notional,
        max_loss_usd=notional,
        reason="favourable: fractional-Kelly target rose above current holding; maker scale-in (add to winner)",
        idempotency_key=f"scalein_{hash_obj(intent_key)[:16]}",
        settlement_cycle=market.get("close_time") or raw_market.get("endDate"),
    )


class ExitManager:
    """For each held position: value it, get win-rate, decide, sell on SELL_SIGNAL."""

    def __init__(
        self,
        *,
        win_provider: WinRateProvider,
        executor: ExecutionAdapter,
        kill_switch: KillSwitch,
        alerter: Alerter,
        risk_buffer: float,
        value_fn: Optional[Callable[[dict], dict]] = None,
    ):
        self.win_provider = win_provider
        self.executor = executor
        self.kill_switch = kill_switch
        self.alerter = alerter
        self.risk_buffer = risk_buffer
        self.value_fn = value_fn if value_fn is not None else self._default_value_fn

    def _default_value_fn(self, config: dict) -> dict:
        return monitor.monitor_tick(config, risk_buffer_default=self.risk_buffer)

    def process_position(self, position_config: dict, *, effective_mode: str,
                         arm_mode: str = "off", buy_halted: bool = False) -> dict:
        ctx = dict(position_config)
        win = self.win_provider.get_win_probability(ctx)
        tick_config = {k: v for k, v in ctx.items() if k != "probability_override"}
        # Feed ANY available estimate (incl in-play low-confidence / market
        # reference) into the break-even valuation — source_class is no longer a
        # gate on exit. The break-even decision decides; a non-edge estimate is
        # labelled (win_rate metadata), not silenced.
        if win.win_probability is not None:
            tick_config["probability_override"] = win.win_probability
        try:
            tick = self.value_fn(tick_config)
        except Exception as exc:  # fail-loud per-position; loop continues
            return {
                "stage": "exit",
                "win_rate": asdict(win),
                "decision": "MARKET_DATA_BLOCKED",
                "reason": str(exc),
                "intent": None,
                "execution": None,
            }

        decision = str(tick.get("decision", {}).get("decision") or "UNKNOWN")
        val = tick.get("valuation", {})
        record: dict = {
            "stage": "exit",
            "win_rate": asdict(win),
            "decision": decision,
            "decision_reason": tick.get("decision", {}).get("reason"),
            "break_even_probability": val.get("break_even_probability"),
            "immediate_exit_value": val.get("immediate_exit_value"),
            "market": tick.get("market", {}).get("slug") or tick.get("market", {}).get("market_id"),
            "intent": None,
            "execution": None,
        }

        # win.source_class is informational metadata now (recorded in win_rate),
        # NOT an exit veto: a break-even SELL fires on any live estimate. Only a
        # TRULY-BLIND position (no usable estimate -> OBSERVE_ONLY) holds, and it
        # alerts loudly instead of going silent — the asleep-and-blind failure
        # mode that lost a World Cup bet overnight.
        record["win_rate"] = asdict(win)

        if decision == "OBSERVE_ONLY":
            record["decision_reason"] = record.get("decision_reason") or (
                "no usable win-rate estimate to compare against break-even; "
                "holding under a loud alert instead of silent observe"
            )
            self.alerter.alert(
                "exit_blind_no_estimate",
                {
                    "market": record.get("market"),
                    "break_even_probability": record.get("break_even_probability"),
                    "immediate_exit_value": record.get("immediate_exit_value"),
                    "win_rate_source_class": win.source_class,
                    "effective_mode": effective_mode,
                },
            )
            return record

        if decision == "CLAIM_REDEEM_SIGNAL":
            market = tick.get("market") if isinstance(tick.get("market"), dict) else {}
            token = tick.get("token") if isinstance(tick.get("token"), dict) else {}
            position = tick.get("position") if isinstance(tick.get("position"), dict) else {}
            settlement = val.get("settlement") if isinstance(val.get("settlement"), dict) else {}
            record["intent"] = {
                "action": "CLAIM_REDEEM",
                "condition_id": market.get("condition_id") or market.get("market_id"),
                "market_slug": market.get("slug"),
                "token_id": token.get("token_id"),
                "side": position.get("side"),
                "shares": position.get("shares"),
                "claimable_value_usd": settlement.get("claimable_value_usd") or val.get("claimable_value_usd"),
                "settlement": settlement,
                "read_only": True,
            }
            record["execution"] = {
                "executed": False,
                "reason": "redeem/claim signal only; live wallet claim requires owner-supervised path",
            }
            self.alerter.alert(
                "exit_claim_redeem_signal",
                {"intent": record["intent"], "effective_mode": effective_mode},
            )
            return record

        if decision == "TRIM_SELL_SIGNAL":
            dec = tick.get("decision", {}) if isinstance(tick.get("decision"), dict) else {}
            trim_shares = to_float(dec.get("trim_shares"))
            record["keep_fraction"] = dec.get("keep_fraction")
            record["trim_shares"] = trim_shares
            if trim_shares is None or trim_shares <= 0:
                record["decision"] = "HOLD"
                record["decision_reason"] = "trim resolved to zero shares; holding full position"
                return record
            intent = build_exit_intent(tick, sell_shares=trim_shares)
            record["intent"] = asdict(intent)
            if self.kill_switch.active():
                record["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
                self.alerter.alert("exit_blocked_killswitch", {"intent": asdict(intent)})
                return record
            result = self.executor.execute(intent, effective_mode=effective_mode)
            record["execution"] = asdict(result)
            self.alerter.alert(
                "exit_trim_sell_signal",
                {"intent": asdict(intent), "effective_mode": effective_mode,
                 "executed": result.executed, "keep_fraction": record.get("keep_fraction")},
            )
            return record

        if decision == "SCALE_IN_SIGNAL":
            # Favourable add-to-winner. A BUY, so it only routes when arm_mode is
            # full (and no buy-halt); exit_only/off record it but never add risk.
            dec = tick.get("decision", {}) if isinstance(tick.get("decision"), dict) else {}
            add_shares = to_float(dec.get("add_shares"))
            record["add_shares"] = add_shares
            record["target_fraction"] = dec.get("target_fraction")
            record["add_notional_usd"] = dec.get("add_notional_usd")
            if arm_mode != "full" or buy_halted:
                record["decision"] = "SCALE_IN_BLOCKED"
                record["decision_reason"] = (
                    f"scale-in needs arm_mode=full and no buy-halt (arm_mode={arm_mode}, buy_halted={buy_halted}); not adding"
                )
                return record
            best_bid = to_float((val.get("depth") or {}).get("best_bid"))
            if add_shares is None or add_shares <= 0 or best_bid is None or best_bid <= 0:
                record["decision"] = "HOLD"
                record["decision_reason"] = "scale-in resolved to zero shares or no maker price; holding"
                return record
            intent = build_scale_in_intent(tick, add_shares=add_shares, maker_price=best_bid)
            record["intent"] = asdict(intent)
            if self.kill_switch.active():
                record["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
                self.alerter.alert("scale_in_blocked_killswitch", {"intent": asdict(intent)})
                return record
            result = self.executor.execute(intent, effective_mode=effective_mode)
            record["execution"] = asdict(result)
            self.alerter.alert(
                "scale_in_signal",
                {"intent": asdict(intent), "effective_mode": effective_mode, "executed": result.executed},
            )
            return record

        if decision in EXIT_DECISIONS_NO_ACTION or decision != "SELL_SIGNAL":
            return record

        intent = build_exit_intent(tick)
        record["intent"] = asdict(intent)

        if self.kill_switch.active():
            record["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
            self.alerter.alert("exit_blocked_killswitch", {"intent": asdict(intent)})
            return record

        result = self.executor.execute(intent, effective_mode=effective_mode)
        record["execution"] = asdict(result)
        self.alerter.alert(
            "exit_sell_signal",
            {"intent": asdict(intent), "effective_mode": effective_mode, "executed": result.executed},
        )
        return record


class EntryManager:
    """Autonomous entry skeleton. Disabled unless a candidate source is wired (S4)
    and the arm-state mode is full. Caps + live gate + edge all apply."""

    def __init__(
        self,
        *,
        win_provider: WinRateProvider,
        executor: ExecutionAdapter,
        cap_guard: CapGuard,
        kill_switch: KillSwitch,
        alerter: Alerter,
        edge_buffer: float,
        fee_bps: float,
        candidate_fn: Optional[Callable[[], list]] = None,
        market_quality: Optional[dict] = None,
        resolution_verifier: Optional[Callable[[dict], dict]] = None,
        maker_pricing: str = DEFAULT_MAKER_PRICING,
        price_expression: str = DEFAULT_PRICE_EXPRESSION_MODE,
    ):
        self.win_provider = win_provider
        self.executor = executor
        self.cap_guard = cap_guard
        self.kill_switch = kill_switch
        self.alerter = alerter
        self.edge_buffer = edge_buffer
        self.fee_bps = fee_bps
        self.candidate_fn = candidate_fn
        self.market_quality = market_quality_policy(market_quality)
        self.market_quality["require_resolution"] = True
        # Which rule prices a post-only BUY. See `maker_limit_price`. Anything other
        # than the literal "queue_aware" keeps the legacy rule — an unknown or
        # misspelled value must never silently change live order prices.
        self.maker_queue_aware = str(maker_pricing or "").strip().lower() == "queue_aware"
        # Price-expression gate. "enforce" makes `rate·px·(1−px)`
        # a first-class term of the buy decision: the per-market fee enters the
        # break-even probability, a non-positive edge-after-fee refuses, and
        # px<0.10 is a no-go zone. "shadow" computes and audits all of it while
        # leaving the decision exactly as it was — money surface, so live
        # enforcement waits on the owner's read of the shadow evidence. Any unknown
        # value keeps shadow: a typo must never silently change live behaviour.
        self.price_expression_enforce = str(price_expression or "").strip().lower() == "enforce"
        # Optional INDEPENDENT clean-resolution verifier. None => disabled (legacy
        # behaviour: only self-attested / packet-attested candidates clear the
        # resolution fuse). When set, it supplies a non-self-attested clean signal
        # to the SAME fuse for candidates that the proposer left unattested.
        self.resolution_verifier = resolution_verifier
        self._attempted_one_shot_ids: set[str] = set()

    def _price_expression(self, candidate: dict, *, entry_price: Any,
                          model_probability: Any, record: dict) -> dict:
        """Price-expression verdict for this order, recorded on every entry path.

        `rate·px·(1−px)` is what the fill actually costs and `rate·(1−px)` is what
        it costs per dollar deployed — the second is why the same 1-point edge is
        worth +15.25% at px=0.05 and −0.50% at px=0.50. Both land in the audit;
        the refusal uses the per-share form because that is the unit `edge` is in.

        Maker (post-only) opens pay zero — the fee term only binds on the in-play
        taker window. The rate is per-market, never a constant."""
        rate, rate_source = _market_fee_rate(candidate.get("market_id"), candidate)
        gate = mgate.price_expression_gate(
            entry_price=entry_price,
            model_probability=model_probability,
            fee_rate=rate,
            maker=not is_speed_window_candidate(candidate),
            fee_rate_source=rate_source,
            floor_override_reason=candidate.get("price_floor_override_reason"),
            enforce=self.price_expression_enforce,
        )
        record["price_expression"] = gate
        return gate

    def _resolution_for_candidate(self, candidate: dict, record: dict) -> bool:
        attestation = resolution_attestation_for_entry(candidate)
        # If the proposer did not (and should not) self-attest a clean resolution,
        # consult the INDEPENDENT verifier: the daemon pulls the market's own
        # authoritative Gamma/UMA resolution metadata and, only if the resolution
        # mechanics are objectively clean, supplies a non-self-attested clean
        # signal to the SAME fuse below. This is trustworthy input, NOT a bypass —
        # everything the fuse already rejects, it still rejects; the verifier fails
        # closed on any fetch error / unrecognised market / non-objective domain.
        if not attestation.get("confirmed_clean_for_gate") and self.resolution_verifier is not None:
            try:
                verdict = self.resolution_verifier(candidate)
            except Exception as exc:  # fail-closed on any verifier error
                verdict = {"clean": False, "reasons": [f"verifier_error:{type(exc).__name__}"]}
            record["resolution_verifier"] = verdict
            if isinstance(verdict, dict) and verdict.get("clean") is True and verdict.get("attestation_source"):
                candidate["resolution_confirmed_clean"] = True
                candidate["resolution_attestation_source"] = verdict["attestation_source"]
                attestation = resolution_attestation_for_entry(candidate)
        record["resolution_attestation"] = attestation
        if attestation.get("self_attested"):
            self.alerter.alert(
                "entry_resolution_self_attested",
                {
                    "candidate_type": attestation.get("candidate_type"),
                    "source": attestation.get("source"),
                    "resolution_source": attestation.get("resolution_source"),
                    "market_id": candidate.get("market_id"),
                    "market_slug": candidate.get("market_slug"),
                    "token_id": candidate.get("token_id"),
                    "idempotency_key": candidate.get("idempotency_key"),
                },
            )
        return bool(attestation.get("confirmed_clean_for_gate"))

    @staticmethod
    def _one_shot_consumed(record: dict) -> bool:
        """Consume one-shot keys only after S1 really accepted the attempt.

        Operational blocks (HALT, kill switch, SDK/proxy failures, bad/expired
        rows) must not burn an MarketFlow intent. S1 idempotency remains the
        authoritative duplicate-order guard after daemon restarts.
        """
        execution = record.get("execution")
        if not isinstance(execution, dict):
            return False
        if execution.get("accepted") is True or execution.get("executed") is True:
            return True
        reason = str(execution.get("reason") or "").lower()
        if "duplicate" in reason and "idempotency" in reason:
            return True
        refusals = (
            ((execution.get("intent") or {}).get("s1") or {})
            .get("fuses", {})
            .get("refusals", [])
        )
        return any(
            "duplicate" in str(item).lower() and "idempotency" in str(item).lower()
            for item in refusals
        )

    def _sync_cap_guard_from_result(self, result: ExecutionResult) -> None:
        if not isinstance(result.intent, dict):
            return
        s1_record = result.intent.get("s1")
        if not isinstance(s1_record, dict):
            return
        fuses = s1_record.get("fuses")
        if not isinstance(fuses, dict):
            return
        self.cap_guard.sync_deployed_floor(fuses.get("deployed_before_usd"))

    def _mark_one_shot_consumed(self, candidate: dict, key: str, record: dict) -> None:
        consumed_path = monitor.safe_str(candidate.get("_intent_consumed_path"))
        if not consumed_path:
            return
        row = {
            "schema_version": AGENT_INTENT_CONSUMED_SCHEMA_VERSION,
            "consumed_at": iso_now(),
            "idempotency_key": key,
            "queue_path": monitor.safe_str(candidate.get("_intent_queue_path")),
            "decision": monitor.safe_str(record.get("decision")),
            "candidate_type": monitor.safe_str(candidate.get("candidate_type")),
            "execution": record.get("execution") if isinstance(record.get("execution"), dict) else None,
        }
        try:
            append_jsonl(consumed_path, row)
        except OSError as exc:
            self.alerter.alert(
                "agent_intent_consumed_write_failed",
                {"idempotency_key": key, "consumed_path": consumed_path, **exception_payload(exc)},
            )

    def tick(self, *, effective_mode: str, allow_buy: bool = True, allow_sell: bool = True) -> list:
        if self.candidate_fn is None:
            return []
        records: list = []
        for candidate in self.candidate_fn():
            if candidate.get("candidate_type") == "entry_source_error":
                records.append(dict(candidate))
                continue
            action = monitor.safe_str(candidate.get("action") or "BUY").upper()
            if action == "BUY" and not allow_buy:
                continue
            if action == "SELL" and not allow_sell:
                continue
            candidate_key = monitor.safe_str(candidate.get("idempotency_key") or candidate.get("intent_id"))
            if bool(candidate.get("one_shot")) and candidate_key in self._attempted_one_shot_ids:
                records.append({
                    "stage": "entry",
                    "candidate_type": monitor.safe_str(candidate.get("candidate_type")),
                    "idempotency_key": candidate_key,
                    "decision": "NO_ENTRY_ONE_SHOT_ALREADY_ATTEMPTED",
                    "intent": None,
                    "execution": None,
                })
                continue
            record = self._process_candidate(candidate, effective_mode=effective_mode)
            records.append(record)
            if bool(candidate.get("one_shot")) and candidate_key and self._one_shot_consumed(record):
                self._attempted_one_shot_ids.add(candidate_key)
                self._mark_one_shot_consumed(candidate, candidate_key, record)
        return records

    def _process_candidate(self, candidate: dict, *, effective_mode: str) -> dict:
        action = monitor.safe_str(candidate.get("action") or "BUY").upper()
        candidate_type = str(candidate.get("candidate_type") or "").strip().lower()
        if action == "SELL" or candidate_type == "agent_exit_intent":
            return self._process_agent_exit_intent(candidate, effective_mode=effective_mode)
        if candidate_type == "agent_intent":
            return self._process_agent_intent(candidate, effective_mode=effective_mode)
        if candidate_type == "supervised_canary":
            return self._process_supervised_canary(candidate, effective_mode=effective_mode)

        win = self.win_provider.get_win_probability(candidate)
        ask = to_float(candidate.get("ask_price"))
        rec: dict = {"stage": "entry", "win_rate": asdict(win), "ask_price": ask, "intent": None, "execution": None}
        # win.source_class (proven-edge vs market-reference) is informational only
        # now — it is recorded in rec["win_rate"], not an entry veto. A
        # self-initiated bet is gated by the OBJECTIVE market-quality filters
        # (band / longshot / resolution) + caps + its own -EV check
        # (entry_should_buy below), never by "is this a proven engine edge".
        gate = mgate.market_quality_gate(
            entry_ask_price=ask,
            model_probability=win.win_probability,
            resolution_confirmed_clean=self._resolution_for_candidate(candidate, rec),
            **self.market_quality,
        )
        rec["quality_gate"] = gate
        if gate["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_QUALITY_GATE_REJECT"
            rec["reject_reason_codes"] = gate["reject_reason_codes"]
            return rec
        pxg = self._price_expression(candidate, entry_price=ask,
                                     model_probability=win.win_probability, record=rec)
        if pxg["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_PRICE_EXPRESSION_REJECT"
            rec["reject_reason_codes"] = pxg["reject_reason_codes"]
            return rec
        if not entry_should_buy(win.win_probability, ask, self.edge_buffer, self.fee_bps,
                                fee_per_share=pxg["fee_per_share"] if self.price_expression_enforce else None):
            rec["decision"] = "NO_ENTRY_NO_EDGE"
            return rec
        maker = maker_limit_price(candidate, executable_price=ask,
                                  queue_aware=self.maker_queue_aware)
        rec["maker_price"] = maker
        if not maker.get("ok"):
            rec["decision"] = "NO_ENTRY_MAKER_PRICE_UNAVAILABLE"
            rec["reason"] = maker.get("reason")
            return rec
        sizing = fractional_kelly_buy_sizing(
            candidate,
            model_probability=win.win_probability,
            executable_price=ask,
            order_price=maker.get("price"),
            cap_guard=self.cap_guard,
        )
        rec["kelly_sizing"] = sizing
        if not sizing.get("ok"):
            rec["decision"] = "NO_ENTRY_SIZING_UNAVAILABLE"
            rec["reason"] = sizing.get("reason")
            return rec
        shares = sizing["shares"]
        notional = sizing["max_spend_usd"]
        cap = self.cap_guard.check_entry(notional)
        rec["cap_check"] = cap
        if not cap["allowed"] or shares <= 0:
            rec["decision"] = "NO_ENTRY_CAP_BLOCKED"
            return rec
        intent = OrderIntent(
            intent_id=f"entry_{now_ms()}_{hash_obj(candidate)[:10]}",
            action="BUY",
            is_exit=False,
            market_id=candidate.get("market_id"),
            market_slug=candidate.get("market_slug"),
            token_id=candidate.get("token_id"),
            side=candidate.get("side"),
            shares=float(shares),
            limit_price=float(maker["price"]),
            est_notional_usd=notional,
            max_loss_usd=notional,
            reason="true win-rate exceeds all-in cost; fractional-Kelly maker entry with exchange-minimum floor",
            order_kind="limit",
            post_only=True,
            settlement_cycle=monitor.safe_str(candidate.get("settlement_cycle") or candidate.get("close_time")),
            price_floor_override_reason=monitor.safe_str(candidate.get("price_floor_override_reason")) or None,
        )
        rec["intent"] = asdict(intent)
        rec["decision"] = "ENTRY_SIGNAL"
        if self.kill_switch.active():
            rec["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
            return rec
        result = self.executor.execute(intent, effective_mode=effective_mode)
        self._sync_cap_guard_from_result(result)
        if result.executed:
            self.cap_guard.record_entry(notional)
        rec["execution"] = asdict(result)
        self.alerter.alert("entry_signal", {"intent": asdict(intent), "effective_mode": effective_mode})
        return rec

    def _process_agent_intent(self, candidate: dict, *, effective_mode: str) -> dict:
        ask = to_float(candidate.get("ask_price") or candidate.get("max_price"))
        shares = to_float(candidate.get("shares"))
        max_spend = to_float(candidate.get("max_spend_usd"))
        max_price = to_float(candidate.get("max_price") or ask)
        max_loss = to_float(candidate.get("max_loss_usd") or max_spend)
        close_time = monitor.safe_str(candidate.get("close_time"))
        rec: dict = {
            "stage": "entry",
            "candidate_type": "agent_intent",
            "ask_price": ask,
            "intent": None,
            "execution": None,
            "close_time": close_time,
            "source": monitor.safe_str(candidate.get("source")),
        }
        expires_at = monitor.safe_str(candidate.get("expires_at"))
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    rec["decision"] = "NO_ENTRY_AGENT_INTENT_EXPIRED"
                    return rec
            except ValueError:
                rec["decision"] = "NO_ENTRY_BAD_AGENT_INTENT_EXPIRY"
                return rec
        if close_time:
            close_dt = parse_close_dt(close_time)
            if close_dt is None:
                rec["decision"] = "NO_ENTRY_BAD_CLOSE_TIME"
                return rec
            if close_dt <= datetime.now(timezone.utc):
                rec["decision"] = "NO_ENTRY_MARKET_CLOSED"
                return rec
        if not bool(candidate.get("allow_live_entry")):
            rec["decision"] = "NO_ENTRY_AGENT_INTENT_NOT_ENABLED"
            return rec
        if monitor.safe_str(candidate.get("action") or "BUY").upper() != "BUY":
            rec["decision"] = "NO_ENTRY_AGENT_INTENT_ACTION_BLOCKED"
            rec["reason"] = "agent-intent path accepts BUY only"
            return rec
        if monitor.safe_str(candidate.get("market_order_type") or "FOK").upper() != "FOK":
            rec["decision"] = "NO_ENTRY_AGENT_INTENT_ORDER_TYPE_BLOCKED"
            rec["reason"] = "agent-intent path accepts FOK only"
            return rec
        if max_price is None or max_price <= 0:
            rec["decision"] = "NO_ENTRY_BAD_AGENT_INTENT"
            return rec
        rec["requested_sizing"] = {
            "shares": shares,
            "max_spend_usd": max_spend,
            "max_loss_usd": max_loss,
        }
        # Re-price off the CURRENT book before any pricing decision (260725). The
        # emitter runs on its own schedule, so an intent's `ask_price` can be minutes
        # to half an hour stale, and everything below keys off it: the band gate, the
        # edge test, Kelly sizing, and the post-only maker price. A stale ask produced
        # post-only BUYs that crossed the live book and were rejected outright.
        # Placed after the expiry/close/type checks so dead intents cost no network.
        # Fail-soft: no book -> keep the intent's own price (previous behaviour).
        live_book = _live_book_quote(candidate.get("token_id"))
        rec["live_book"] = live_book or None
        if live_book.get("best_ask") is not None:
            candidate = dict(candidate)
            candidate["ask_price"] = live_book["best_ask"]
            if live_book.get("best_bid") is not None:
                candidate["best_bid"] = live_book["best_bid"]
            if live_book.get("tick_size") is not None:
                candidate.setdefault("tick_size", live_book["tick_size"])
            # Carry the full depth through: queue-aware maker pricing reads the size
            # at every level, and the top-of-book fields above do not carry it.
            for _lv in ("bid_levels", "ask_levels"):
                if live_book.get(_lv):
                    candidate[_lv] = live_book[_lv]
            ask = to_float(live_book["best_ask"])
            rec["ask_price"] = ask
            rec["ask_price_source"] = "live_book"
            # The intent's own max_price is the emitter's stated willingness to pay.
            # If the live book has run past it, the trade the emitter authorised no
            # longer exists — refuse rather than chase.
            if max_price is not None and ask is not None and ask > max_price + 1e-9:
                rec["decision"] = "NO_ENTRY_PRICE_MOVED_PAST_INTENT_MAX"
                rec["reason"] = f"live ask {ask} exceeds intent max_price {max_price}"
                return rec
        else:
            rec["ask_price_source"] = "intent_stale"
        # Objective filters bind before caps / arm-state / S1. MarketFlow-directed
        # entries also need private probability to clear all-in cost; otherwise
        # they are just no-edge taker/maker churn.
        gate = mgate.market_quality_gate(
            entry_ask_price=ask if ask is not None else max_price,
            model_probability=candidate.get("model_probability"),
            resolution_confirmed_clean=self._resolution_for_candidate(candidate, rec),
            **self.market_quality,
        )
        rec["quality_gate"] = gate
        if gate["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_QUALITY_GATE_REJECT"
            rec["reject_reason_codes"] = gate["reject_reason_codes"]
            return rec
        edge_price = ask if ask is not None else max_price
        pxg = self._price_expression(candidate, entry_price=edge_price,
                                     model_probability=candidate.get("model_probability"), record=rec)
        if pxg["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_PRICE_EXPRESSION_REJECT"
            rec["reject_reason_codes"] = pxg["reject_reason_codes"]
            return rec
        if not entry_should_buy(candidate.get("model_probability"), edge_price, self.edge_buffer, self.fee_bps,
                                fee_per_share=pxg["fee_per_share"] if self.price_expression_enforce else None):
            rec["decision"] = "NO_ENTRY_NO_EDGE"
            rec["reason"] = "model probability does not clear executable price plus all-in edge buffer"
            return rec
        speed_window = is_speed_window_candidate(candidate)
        maker = maker_limit_price(candidate, executable_price=edge_price,
                                  queue_aware=self.maker_queue_aware)
        rec["maker_price"] = maker
        if not speed_window and not maker.get("ok"):
            rec["decision"] = "NO_ENTRY_MAKER_PRICE_UNAVAILABLE"
            rec["reason"] = maker.get("reason")
            return rec
        order_price = max_price if speed_window else maker.get("price")
        sizing = fractional_kelly_buy_sizing(
            candidate,
            model_probability=candidate.get("model_probability"),
            executable_price=edge_price,
            order_price=order_price,
            cap_guard=self.cap_guard,
        )
        rec["kelly_sizing"] = sizing
        if not sizing.get("ok"):
            rec["decision"] = "NO_ENTRY_SIZING_UNAVAILABLE"
            rec["reason"] = sizing.get("reason")
            return rec
        shares = sizing["shares"]
        max_spend = sizing["max_spend_usd"]
        max_loss = max_spend
        cap = self.cap_guard.check_entry(max_spend)
        rec["cap_check"] = cap
        if not cap["allowed"]:
            rec["decision"] = "NO_ENTRY_CAP_BLOCKED"
            return rec
        intent = OrderIntent(
            intent_id=monitor.safe_str(candidate.get("idempotency_key") or f"agent_{now_ms()}_{hash_obj(candidate)[:10]}"),
            action="BUY",
            is_exit=False,
            market_id=monitor.safe_str(candidate.get("market_id")),
            market_slug=monitor.safe_str(candidate.get("market_slug")),
            token_id=monitor.safe_str(candidate.get("token_id")),
            side=monitor.safe_str(candidate.get("side") or "YES"),
            shares=float(shares),
            limit_price=None if speed_window else float(order_price),
            est_notional_usd=float(max_spend),
            max_loss_usd=float(max_loss),
            reason=monitor.safe_str(candidate.get("reason") or "MarketFlow edge-gated fractional-Kelly entry intent"),
            order_kind="market" if speed_window else "limit",
            market_order_type="FOK",
            post_only=not speed_window,
            taker_allowed_reason=SPEED_WINDOW_TAKER_REASON if speed_window else None,
            max_spend_usd=float(max_spend) if speed_window else None,
            max_price=float(max_price) if speed_window else None,
            idempotency_key=monitor.safe_str(candidate.get("idempotency_key")),
            settlement_cycle=monitor.safe_str(candidate.get("settlement_cycle") or close_time),
            price_floor_override_reason=monitor.safe_str(candidate.get("price_floor_override_reason")) or None,
        )
        rec["intent"] = asdict(intent)
        rec["decision"] = "AGENT_INTENT_ENTRY_SIGNAL"
        if self.kill_switch.active():
            rec["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
            return rec
        result = self.executor.execute(intent, effective_mode=effective_mode)
        self._sync_cap_guard_from_result(result)
        if result.executed:
            self.cap_guard.record_entry(max_spend)
        rec["execution"] = asdict(result)
        self.alerter.alert("agent_intent_entry_signal", {"intent": asdict(intent), "effective_mode": effective_mode})
        return rec

    def _process_agent_exit_intent(self, candidate: dict, *, effective_mode: str) -> dict:
        shares = to_float(candidate.get("shares"))
        held_shares = to_float(candidate.get("held_shares"))
        min_price = to_float(candidate.get("min_price"))
        rec: dict = {
            "stage": "exit",
            "candidate_type": "agent_exit_intent",
            "intent": None,
            "execution": None,
            "source": monitor.safe_str(candidate.get("source")),
            "min_price": min_price,
        }
        expires_at = monitor.safe_str(candidate.get("expires_at"))
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp_dt <= datetime.now(timezone.utc):
                    rec["decision"] = "NO_EXIT_AGENT_INTENT_EXPIRED"
                    return rec
            except ValueError:
                rec["decision"] = "NO_EXIT_BAD_AGENT_INTENT_EXPIRY"
                return rec
        if not bool(candidate.get("allow_live_exit")):
            rec["decision"] = "NO_EXIT_AGENT_INTENT_NOT_ENABLED"
            return rec
        order_type = monitor.safe_str(candidate.get("market_order_type") or "FAK").upper()
        if order_type not in ("FAK", "FOK"):
            rec["decision"] = "NO_EXIT_AGENT_INTENT_ORDER_TYPE_BLOCKED"
            rec["reason"] = "agent-exit path accepts FAK or FOK"
            return rec
        if shares is None or shares <= 0 or held_shares is None or held_shares <= 0:
            rec["decision"] = "NO_EXIT_BAD_AGENT_INTENT"
            rec["reason"] = "SELL requires positive shares and held_shares"
            return rec
        if shares > held_shares + 1e-9:
            rec["decision"] = "NO_EXIT_NAKED_SHORT_BLOCKED"
            rec["reason"] = f"shares {shares} exceeds held_shares {held_shares}"
            return rec
        if min_price is None or min_price <= 0 or min_price > 1:
            rec["decision"] = "NO_EXIT_BAD_MIN_PRICE"
            rec["reason"] = "market SELL requires min_price in (0,1]"
            return rec
        intent = OrderIntent(
            intent_id=monitor.safe_str(candidate.get("idempotency_key") or f"agent_exit_{now_ms()}_{hash_obj(candidate)[:10]}"),
            action="SELL",
            is_exit=True,
            market_id=monitor.safe_str(candidate.get("market_id")),
            market_slug=monitor.safe_str(candidate.get("market_slug")),
            token_id=monitor.safe_str(candidate.get("token_id")),
            side=monitor.safe_str(candidate.get("side") or "HELD"),
            shares=float(shares),
            held_shares=float(held_shares),
            limit_price=None,
            est_notional_usd=round(float(shares) * float(min_price), 8),
            max_loss_usd=None,
            reason=monitor.safe_str(candidate.get("reason") or "MarketFlow chat-generated bounded reduce/exit intent"),
            order_kind="market",
            market_order_type=order_type,
            min_price=float(min_price),
            idempotency_key=monitor.safe_str(candidate.get("idempotency_key")),
            settlement_cycle=monitor.safe_str(candidate.get("settlement_cycle") or candidate.get("close_time")),
        )
        rec["intent"] = asdict(intent)
        rec["decision"] = "AGENT_INTENT_EXIT_SIGNAL"
        if self.kill_switch.active():
            rec["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
            return rec
        result = self.executor.execute(intent, effective_mode=effective_mode)
        rec["execution"] = asdict(result)
        self.alerter.alert("agent_intent_exit_signal", {"intent": asdict(intent), "effective_mode": effective_mode})
        return rec

    def _process_supervised_canary(self, candidate: dict, *, effective_mode: str) -> dict:
        ask = to_float(candidate.get("ask_price") or candidate.get("max_price"))
        shares = to_float(candidate.get("shares"))
        max_spend = to_float(candidate.get("max_spend_usd"))
        max_price = to_float(candidate.get("max_price") or ask)
        max_loss = to_float(candidate.get("max_loss_usd") or max_spend)
        close_time = monitor.safe_str(candidate.get("close_time"))
        rec: dict = {
            "stage": "entry",
            "candidate_type": "supervised_canary",
            "ask_price": ask,
            "intent": None,
            "execution": None,
            "close_time": close_time,
        }
        if close_time:
            close_dt = parse_close_dt(close_time)
            if close_dt is None:
                rec["decision"] = "NO_ENTRY_BAD_CLOSE_TIME"
                return rec
            if close_dt <= datetime.now(timezone.utc):
                rec["decision"] = "NO_ENTRY_MARKET_CLOSED"
                return rec
        if not bool(candidate.get("allow_live_entry")):
            rec["decision"] = "NO_ENTRY_CANARY_NOT_ENABLED"
            return rec
        if max_price is None or max_price <= 0:
            rec["decision"] = "NO_ENTRY_BAD_CANARY_PACKET"
            return rec
        rec["requested_sizing"] = {
            "shares": shares,
            "max_spend_usd": max_spend,
            "max_loss_usd": max_loss,
        }
        # the owner supervises the <=$1 canary, but it still uses the same objective
        # gate as autonomous entries: clean resolution, model probability, and
        # all-in edge must be present before any order intent is built.
        gate = mgate.market_quality_gate(
            entry_ask_price=ask if ask is not None else max_price,
            model_probability=candidate.get("model_probability"),
            resolution_confirmed_clean=self._resolution_for_candidate(candidate, rec),
            **self.market_quality,
        )
        rec["quality_gate"] = gate
        if gate["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_QUALITY_GATE_REJECT"
            rec["reject_reason_codes"] = gate["reject_reason_codes"]
            return rec
        edge_price = ask if ask is not None else max_price
        pxg = self._price_expression(candidate, entry_price=edge_price,
                                     model_probability=candidate.get("model_probability"), record=rec)
        if pxg["authorization"] != "APPROVED":
            rec["decision"] = "NO_ENTRY_PRICE_EXPRESSION_REJECT"
            rec["reject_reason_codes"] = pxg["reject_reason_codes"]
            return rec
        if not entry_should_buy(candidate.get("model_probability"), edge_price, self.edge_buffer, self.fee_bps,
                                fee_per_share=pxg["fee_per_share"] if self.price_expression_enforce else None):
            rec["decision"] = "NO_ENTRY_NO_EDGE"
            rec["reason"] = "model probability does not clear executable price plus all-in edge buffer"
            return rec
        speed_window = is_speed_window_candidate(candidate)
        maker = maker_limit_price(candidate, executable_price=edge_price,
                                  queue_aware=self.maker_queue_aware)
        rec["maker_price"] = maker
        if not speed_window and not maker.get("ok"):
            rec["decision"] = "NO_ENTRY_MAKER_PRICE_UNAVAILABLE"
            rec["reason"] = maker.get("reason")
            return rec
        order_price = max_price if speed_window else maker.get("price")
        sizing = fractional_kelly_buy_sizing(
            candidate,
            model_probability=candidate.get("model_probability"),
            executable_price=edge_price,
            order_price=order_price,
            cap_guard=self.cap_guard,
        )
        rec["kelly_sizing"] = sizing
        if not sizing.get("ok"):
            rec["decision"] = "NO_ENTRY_SIZING_UNAVAILABLE"
            rec["reason"] = sizing.get("reason")
            return rec
        shares = sizing["shares"]
        max_spend = sizing["max_spend_usd"]
        max_loss = max_spend
        cap = self.cap_guard.check_entry(max_spend)
        rec["cap_check"] = cap
        if not cap["allowed"]:
            rec["decision"] = "NO_ENTRY_CAP_BLOCKED"
            return rec
        intent = OrderIntent(
            intent_id=monitor.safe_str(candidate.get("idempotency_key") or f"canary_{now_ms()}_{hash_obj(candidate)[:10]}"),
            action="BUY",
            is_exit=False,
            market_id=monitor.safe_str(candidate.get("market_id")),
            market_slug=monitor.safe_str(candidate.get("market_slug")),
            token_id=monitor.safe_str(candidate.get("token_id")),
            side=monitor.safe_str(candidate.get("side") or "YES"),
            shares=float(shares),
            limit_price=None if speed_window else float(order_price),
            est_notional_usd=float(max_spend),
            max_loss_usd=float(max_loss),
            reason=monitor.safe_str(candidate.get("reason") or "owner-supervised edge-gated <=$1 canary entry"),
            order_kind="market" if speed_window else "limit",
            market_order_type=monitor.safe_str(candidate.get("market_order_type") or "FOK").upper(),
            post_only=not speed_window,
            taker_allowed_reason=SPEED_WINDOW_TAKER_REASON if speed_window else None,
            max_spend_usd=float(max_spend) if speed_window else None,
            max_price=float(max_price) if speed_window else None,
            idempotency_key=monitor.safe_str(candidate.get("idempotency_key")),
            settlement_cycle=monitor.safe_str(candidate.get("settlement_cycle") or close_time),
            price_floor_override_reason=monitor.safe_str(candidate.get("price_floor_override_reason")) or None,
        )
        rec["intent"] = asdict(intent)
        rec["decision"] = "SUPERVISED_CANARY_ENTRY_SIGNAL"
        if self.kill_switch.active():
            rec["execution"] = {"executed": False, "reason": "kill switch active; execution halted"}
            return rec
        result = self.executor.execute(intent, effective_mode=effective_mode)
        self._sync_cap_guard_from_result(result)
        if result.executed:
            self.cap_guard.record_entry(max_spend)
        rec["execution"] = asdict(result)
        self.alerter.alert("supervised_canary_entry_signal", {"intent": asdict(intent), "effective_mode": effective_mode})
        return rec


def load_daemon_config(path: Optional[str]) -> dict:
    if not path:
        return default_config()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise DaemonError("daemon config must be a JSON object")
    monitor.validate_no_sensitive_config(data)
    merged = default_config()
    merged.update(data)
    return merged


def default_config() -> dict:
    return {
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "risk_buffer": DEFAULT_RISK_BUFFER,
        "edge_buffer": DEFAULT_EDGE_BUFFER,
        "fee_bps": DEFAULT_FEE_BPS,
        "capital": {"total_cap_usd": DEFAULT_TOTAL_CAP_USD, "per_trade_cap_usd": DEFAULT_PER_TRADE_CAP_USD},
        "position_source": "config",
        "position_discovery_timeout_sec": 20.0,
        "positions": [],
        "win_rate_provider": "stub",
        "alert": {"enabled": False},
        "market_quality": dict(DEFAULT_MARKET_QUALITY),
        "autonomous_turn": {
            "enabled": False,
            "bridge_url": DEFAULT_BRIDGE_AUTONOMOUS_URL,
            "timeout_sec": 180,
            "poll_sec": 2,
            "trigger_on_tick": False,
            "min_interval_sec": 900,
            "dry_run_only": True,
            "ledger_path": trade_experience.DEFAULT_LEDGER,
        },
        "enable_compiler_intents": True,
        "compiler_intent_queue_files": [DEFAULT_AGENT_INTENT_QUEUE],
        "compiler_intent_queue_tail": 25,
        # Independent clean-resolution verifier (read-only Gamma/UMA metadata).
        # Enabled by default: it is itself a fail-closed safety component that only
        # confirms objective (sports/esports) + standard-UMA + undisputed markets;
        # it never arms anything, never weakens caps/kill/arm-state.
        "resolution_verifier": {"enabled": True, "fetch_timeout_sec": rverify.DEFAULT_FETCH_TIMEOUT},
    }


def cap_consistency_report(config_path: Optional[str] = None) -> dict[str, Any]:
    source_total = pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD
    source_per_trade = pmx.DEFAULT_MAX_PER_TRADE_USD
    default_cap = default_config().get("capital", {})
    s1_default = S1ExecutionAdapter({})._resolve(pmx)
    config_cap: dict[str, Any] = {}
    config_s1: dict[str, Any] = {}
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            cap = cfg.get("capital", {})
            s1 = cfg.get("s1", {})
            config_cap = cap if isinstance(cap, dict) else {}
            config_s1 = s1 if isinstance(s1, dict) else {}
    values = {
        "source_total_cap_usd": source_total,
        "source_per_trade_cap_usd": source_per_trade,
        "daemon_default_total_cap_usd": default_cap.get("total_cap_usd"),
        "daemon_default_per_trade_cap_usd": default_cap.get("per_trade_cap_usd"),
        "s1_default_total_cap_usd": s1_default.get("max_total_deploy_usd"),
        "s1_default_per_trade_cap_usd": s1_default.get("max_per_trade_usd"),
        "config_capital_total_cap_usd": config_cap.get("total_cap_usd", source_total),
        "config_capital_per_trade_cap_usd": config_cap.get("per_trade_cap_usd", source_per_trade),
        "config_s1_total_cap_usd": config_s1.get("max_total_deploy_usd", source_total),
        "config_s1_per_trade_cap_usd": config_s1.get("max_per_trade_usd", source_per_trade),
    }
    ok = (
        abs(float(values["daemon_default_total_cap_usd"]) - source_total) < 1e-9
        and abs(float(values["daemon_default_per_trade_cap_usd"]) - source_per_trade) < 1e-9
        and abs(float(values["s1_default_total_cap_usd"]) - source_total) < 1e-9
        and abs(float(values["s1_default_per_trade_cap_usd"]) - source_per_trade) < 1e-9
        and abs(float(values["config_capital_total_cap_usd"]) - source_total) < 1e-9
        and abs(float(values["config_capital_per_trade_cap_usd"]) - source_per_trade) < 1e-9
        and abs(float(values["config_s1_total_cap_usd"]) - source_total) < 1e-9
        and abs(float(values["config_s1_per_trade_cap_usd"]) - source_per_trade) < 1e-9
    )
    return {"ok": ok, "values": values, "config_path": config_path}


def _position_discovery_meta(
    *,
    source: str,
    positions: list[dict[str, Any]],
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out = dict(meta or {})
    out.setdefault("source", source)
    out["positions_count"] = len(positions)
    out["sdk_confirmed_empty"] = len(positions) == 0
    out.setdefault("truncated_by_max_positions", False)
    if out.get("truncated_by_max_positions"):
        total = out.get("total_count")
        out["truncated_count"] = max(0, int(total) - len(positions)) if isinstance(total, int) else None
    return out


def discover_positions_with_meta(
    config: dict,
    *,
    secret_dir: str,
    alerter: Optional[Alerter] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = str(config.get("position_source") or "config").strip()
    if source == "config":
        positions = config.get("positions") or []
        if not isinstance(positions, list):
            raise DaemonError("config.positions must be a list")
        configs = [dict(p) for p in positions]
        return configs, _position_discovery_meta(source=source, positions=configs)
    if source == "live_account":
        timeout_sec = float(config.get("position_discovery_timeout_sec", 20.0))

        def _fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            return monitor.fetch_private_open_position_configs(
                secret_dir=secret_dir,
                page_size=int(config.get("page_size", 20)),
                max_positions=sys.maxsize,
                probability_override=None,
                probability_override_side=None,
                risk_buffer=float(config.get("risk_buffer", DEFAULT_RISK_BUFFER)),
            )

        configs, _meta = _with_hard_timeout(
            timeout_sec,
            "live_account position discovery",
            _fetch,
        )
        meta = _position_discovery_meta(source=source, positions=configs, meta=_meta)
        if "max_positions" in config:
            meta["configured_max_positions"] = config.get("max_positions")
            meta["configured_max_positions_ignored_for_exit"] = True
        if meta.get("truncated_by_max_positions"):
            if alerter is not None:
                alerter.alert("position_discovery_truncated", meta)
        return configs, meta
    raise DaemonError(f"unknown position_source: {source}")


def discover_positions(config: dict, *, secret_dir: str) -> list:
    positions, _meta = discover_positions_with_meta(config, secret_dir=secret_dir)
    return positions


def read_available_collateral_usd(
    *,
    secret_dir: str,
    timeout_sec: float = 15.0,
) -> Optional[float]:
    """Spendable COLLATERAL on the live account, in USD. None when unknown.

    Why the daemon needs this (260725): caps are a POLICY ceiling ("how much are
    we allowed to deploy"); they say nothing about how much the wallet actually
    holds. Sizing that only reads caps builds every order at the per-trade cap,
    and the exchange rejects it — every entry rejected for insufficient balance,
    i.e. the whole entry path silently spinning on a sub-dollar gap.

    Read-only (`get_balance_allowance`), never signs, never logs a secret value.
    Fail-soft by design: any error returns None and sizing falls back to its
    cap-only behaviour with the exchange as backstop — a balance read must never
    be able to block trading."""
    try:
        secrets = pmx.load_polymarket_secret_refs(secret_dir)
        client = _with_hard_timeout(
            timeout_sec,
            "balance read SecureClient build",
            lambda: pmx.build_secure_client(secrets, secret_dir=secret_dir, side="BUY"),
        )
        try:
            ba = _with_hard_timeout(
                timeout_sec,
                "get_balance_allowance",
                lambda: client.get_balance_allowance(asset_type="COLLATERAL"),
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
        raw = getattr(ba, "balance", None)
        if raw is None and isinstance(ba, dict):
            raw = ba.get("balance")
        val = to_float(raw)
        if val is None or val < 0:
            return None
        # CLOB reports collateral in base units (6 decimals).
        return round(val / 1_000_000.0, 6)
    except Exception:
        return None


def _candidate_from_supervised_canary_packet(packet: dict, *, allow_live_entry: bool) -> dict:
    market = packet.get("market") or {}
    token = packet.get("token") or {}
    rules = packet.get("market_rules_readback") or {}
    buy = packet.get("buy_leg") or {}
    if packet.get("schema_version") != "polymarket-supervised-canary-packet-v0.1":
        raise DaemonError("unsupported supervised canary packet schema")
    return {
        "candidate_type": "supervised_canary",
        "allow_live_entry": bool(allow_live_entry),
        "market_id": market.get("condition_id") or market.get("market_id"),
        "market_slug": market.get("market_slug"),
        "token_id": token.get("token_id"),
        "side": token.get("side") or "YES",
        "ask_price": rules.get("best_ask") or buy.get("max_price"),
        "best_bid": rules.get("best_bid"),
        "shares": buy.get("target_size_shares"),
        "max_spend_usd": buy.get("max_spend_usd"),
        "max_price": buy.get("max_price"),
        "max_loss_usd": buy.get("max_loss_usd"),
        "order_min_size": rules.get("order_min_size_shares") or rules.get("order_min_size"),
        "tick_size": rules.get("tick_size"),
        "model_probability": buy.get("model_probability") or packet.get("model_probability"),
        "resolution_confirmed_clean": market.get("clean_resolution") is True,
        "resolution_attestation_source": "supervised_canary_packet_market_clean_resolution",
        "market_order_type": buy.get("market_order_type") or "FOK",
        "taker_allowed_reason": buy.get("taker_allowed_reason"),
        "speed_window": buy.get("speed_window") or buy.get("speed_window_candidate"),
        "idempotency_key": buy.get("idempotency_key"),
        "close_time": market.get("close_time"),
        "reason": "owner-supervised exchange-minimum BUY canary from packet",
    }


def _entry_source_error_record(*, source: str, path: str, exc: BaseException) -> dict:
    return {
        "stage": "entry",
        "candidate_type": "entry_source_error",
        "decision": "ENTRY_SOURCE_ERROR",
        "source": source,
        "path": path,
        "intent": None,
        "execution": None,
        **exception_payload(exc),
    }


def _load_agent_intent_queue(
    path: str,
    *,
    allow_live_entry: bool,
    allow_live_exit: Optional[bool] = None,
    limit: int = 25,
    alerter: Optional[Alerter] = None,
    source_filter: Optional[set[str]] = None,
    source_exclude: Optional[set[str]] = None,
) -> list[dict]:
    if not path:
        return []
    if not os.path.isabs(path):
        path = os.path.join(REPO_ROOT, path)
    if not os.path.isfile(path):
        return []
    consumed_path = _agent_intent_consumed_path(path)
    try:
        consumed_keys = _load_agent_intent_consumed_keys(consumed_path)
    except OSError as exc:
        record = _entry_source_error_record(source="agent_intent_consumed", path=consumed_path, exc=exc)
        if alerter is not None:
            alerter.alert("agent_intent_consumed_read_failed", record)
        return [record]
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        record = _entry_source_error_record(source="agent_intent_queue", path=path, exc=exc)
        if alerter is not None:
            alerter.alert("agent_intent_queue_read_failed", record)
        return [record]
    for line in lines[-max(1, min(int(limit or 25), 200)):]:
        raw = line.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            payload = exception_payload(exc, source="agent_intent_queue", path=path)
            if alerter is not None:
                alerter.alert("agent_intent_queue_bad_json", payload)
            rows.append({
                "stage": "entry",
                "candidate_type": "entry_source_error",
                "allow_live_entry": False,
                "decision": "NO_ENTRY_BAD_AGENT_INTENT_JSON",
                "reason": "bad JSONL row",
                "intent": None,
                "execution": None,
                **payload,
            })
            continue
        if not isinstance(row, dict):
            continue
        if row.get("schema_version") != "polymarket-agent-intent-v0.1":
            continue
        source = monitor.safe_str(row.get("source")) or ""
        if source_filter is not None and source not in source_filter:
            continue
        if source_exclude is not None and source in source_exclude:
            continue
        key = monitor.safe_str(row.get("idempotency_key") or row.get("intent_id"))
        if key and key in consumed_keys:
            continue
        candidate = dict(row)
        action = monitor.safe_str(row.get("action") or "BUY").upper()
        candidate["candidate_type"] = "agent_exit_intent" if action == "SELL" else "agent_intent"
        if action == "SELL":
            exit_allowed = allow_live_entry if allow_live_exit is None else allow_live_exit
            candidate["allow_live_exit"] = bool(exit_allowed and row.get("allow_live_exit"))
        else:
            candidate["allow_live_entry"] = bool(allow_live_entry and row.get("allow_live_entry"))
        candidate.setdefault("one_shot", True)
        candidate["_intent_queue_path"] = path
        candidate["_intent_consumed_path"] = consumed_path
        rows.append(candidate)
    return rows


def make_entry_candidate_fn(config: dict, *, alerter: Optional[Alerter] = None) -> Optional[Callable[[], list]]:
    candidates: list[dict] = []
    for item in config.get("entry_candidates") or []:
        if not isinstance(item, dict):
            raise DaemonError("entry_candidates must contain JSON objects")
        candidates.append(dict(item))
    allow_canary = bool(config.get("enable_supervised_canary_entries"))
    for path in config.get("supervised_canary_packet_files") or []:
        with open(path, encoding="utf-8") as f:
            packet = json.load(f)
        if not isinstance(packet, dict):
            raise DaemonError(f"supervised canary packet is not a JSON object: {path}")
        candidates.append(_candidate_from_supervised_canary_packet(packet, allow_live_entry=allow_canary))
    agent_paths = list(config.get("agent_intent_queue_files") or [])
    allow_agent = bool(config.get("enable_agent_intent_entries"))
    agent_limit = int(config.get("agent_intent_queue_tail", 25))
    compiler_paths = list(config.get("compiler_intent_queue_files") or [])
    allow_compiler = bool(config.get("enable_compiler_intents"))
    compiler_limit = int(config.get("compiler_intent_queue_tail", agent_limit))
    if not candidates and not agent_paths and not compiler_paths:
        return None

    def _load() -> list:
        out = [dict(c) for c in candidates]
        for path in compiler_paths:
            out.extend(_load_agent_intent_queue(
                str(path),
                allow_live_entry=allow_compiler,
                allow_live_exit=allow_compiler,
                limit=compiler_limit,
                alerter=alerter,
                source_filter={"edge_compiler"},
            ))
        for path in agent_paths:
            out.extend(_load_agent_intent_queue(
                str(path),
                allow_live_entry=allow_agent,
                allow_live_exit=allow_agent,
                limit=agent_limit,
                alerter=alerter,
                source_exclude={"edge_compiler"},
            ))
        return out

    return _load


def make_resolution_verifier(
    config: dict, *, cache: Optional[dict] = None
) -> Optional[Callable[[dict], dict]]:
    """Build the independent clean-resolution verifier callable from config.

    Returns None (disabled) when `resolution_verifier.enabled` is false. The
    callable is read-only (public Gamma metadata), fail-closed, and only ever
    SUPPLIES an independent clean attestation — it never touches caps / arm-state
    / kill / secrets and cannot weaken any other fuse.
    """
    cfg = config.get("resolution_verifier") if isinstance(config.get("resolution_verifier"), dict) else {}
    if not bool(cfg.get("enabled", True)):
        return None
    timeout = float(cfg.get("fetch_timeout_sec", rverify.DEFAULT_FETCH_TIMEOUT))

    def _verify(candidate: dict) -> dict:
        return rverify.verify_candidate(candidate, timeout=timeout, cache=cache)

    return _verify


def make_win_provider(name: str, config: dict) -> WinRateProvider:
    key = (name or "stub").strip().lower()
    if key in ("stub", "probability_override"):
        return StubWinRateProvider()
    raise DaemonError(f"unknown win_rate_provider: {name}")


def make_executor(name: str, config: dict, *, alerter: Optional[Alerter] = None) -> ExecutionAdapter:
    key = (name or "stub").strip().lower()
    if key in ("stub", "dry_run", "dry_run_stub"):
        return DryRunStubExecutionAdapter()
    if key in ("s1", "polymarket_execution"):
        return S1ExecutionAdapter(config, alerter=alerter)
    raise DaemonError(f"unknown executor: {name}")


def _decision_market(decision: dict) -> dict:
    market = decision.get("market")
    return market if isinstance(market, dict) else {}


def _decision_intent(decision: dict) -> dict:
    intent = decision.get("intent")
    return intent if isinstance(intent, dict) else {}


class AutotradeDaemon:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config = load_daemon_config(args.config)
        self.instance_lock = SingleInstanceLock(args.lock_file)
        if args.interval is not None:
            self.config["interval_seconds"] = args.interval
        if args.risk_buffer is not None:
            self.config["risk_buffer"] = args.risk_buffer
        cap = self.config.get("capital", {})
        self.cap_guard = CapGuard(
            total_cap_usd=float(cap.get("total_cap_usd", DEFAULT_TOTAL_CAP_USD)),
            per_trade_cap_usd=float(cap.get("per_trade_cap_usd", DEFAULT_PER_TRADE_CAP_USD)),
        )
        self.live_gate = LiveGate(requested_live=args.live, arm_state_file=args.arm_state_file)
        self.kill_switch = KillSwitch(kill_file=args.kill_file)
        alert_cfg = self.config.get("alert", {})
        self.alerter = Alerter(enabled=bool(alert_cfg.get("enabled")), alert_file=args.alert_file)
        win_name = args.win_provider or self.config.get("win_rate_provider") or "stub"
        exec_name = args.executor or self.config.get("executor") or "stub"
        self.win_provider: WinRateProvider = make_win_provider(win_name, self.config.get("s2", {}))
        self.executor: ExecutionAdapter = make_executor(exec_name, self.config.get("s1", {}), alerter=self.alerter)
        self.autonomous_turn = AutonomousTurnClient(self.config.get("autonomous_turn", {}))
        self.experience_ledger_path = self.autonomous_turn.ledger_path
        self.exit_manager = ExitManager(
            win_provider=self.win_provider,
            executor=self.executor,
            kill_switch=self.kill_switch,
            alerter=self.alerter,
            risk_buffer=float(self.config.get("risk_buffer", DEFAULT_RISK_BUFFER)),
        )
        self._resolution_cache: dict[str, dict] = {}
        # Running high-water-mark of the sell price per held position (in-memory,
        # per process). The take-profit trim only fires on a FRESH high, which is
        # what makes partial trimming convergent (a stable price is not re-trimmed
        # toward zero). A restart resets peaks harmlessly (at most one extra trim).
        self._exit_peaks: dict[str, float] = {}
        # Escalation latch for repeated exit-evaluation failures. Per-position
        # failures previously went only to a local jsonl while alerting covered
        # just the kill switch and the global halt, so one position's sell decision
        # could fail hundreds of times over half a day with nobody told. A position
        # that cannot see whether it should be sold is blind on the money path, and
        # somebody has to be woken.
        self._exit_fail_streak: dict[str, int] = {}
        self._exit_fail_notified: set[str] = set()
        self.entry_manager = EntryManager(
            win_provider=self.win_provider,
            executor=self.executor,
            cap_guard=self.cap_guard,
            kill_switch=self.kill_switch,
            alerter=self.alerter,
            edge_buffer=float(self.config.get("edge_buffer", DEFAULT_EDGE_BUFFER)),
            fee_bps=float(self.config.get("fee_bps", DEFAULT_FEE_BPS)),
            candidate_fn=make_entry_candidate_fn(self.config, alerter=self.alerter),
            market_quality=self.config.get("market_quality"),
            resolution_verifier=make_resolution_verifier(self.config, cache=self._resolution_cache),
            maker_pricing=self.config.get("maker_pricing", DEFAULT_MAKER_PRICING),
            price_expression=self.config.get("price_expression", DEFAULT_PRICE_EXPRESSION_MODE),
        )

    def _load_experience_metrics(self, record: dict) -> None:
        try:
            record["experience_metrics"] = trade_experience.metrics(ledger_path=self.experience_ledger_path)
        except Exception as exc:
            payload = exception_payload(exc, ledger_path=self.experience_ledger_path)
            record["experience_metrics_error"] = payload
            record["experience_metrics"] = {
                "schema_version": "trade-experience-metrics-v0.1",
                "generated_at": iso_now(),
                "principal_usd": trade_experience.PRINCIPAL_USD,
                "snowball_nav_usd": trade_experience.PRINCIPAL_USD,
                "realized_pnl_usd": 0.0,
                "settled_count": 0,
                "scored_count": 0,
                "win_rate": None,
                "brier_score": None,
                "reward_signal": "forward_calibration_oos_win_rate_drawdown_adjusted_snowball_slope_not_single_trade_pnl",
                "source": "empty_due_to_read_error",
            }
            self.alerter.alert("experience_metrics_failed", payload)

    # ---- escalation for repeated exit-evaluation failures ----
    # Per-position isolation already existed: one position failing does not affect
    # the others. What was missing is **escalation** — failures landed in a local
    # file and woke nobody. A position that repeatedly cannot decide whether to sell
    # is blind on the money path, and unlike a venue rejection it never even reaches
    # the order step. The threshold is a handful of consecutive failures, which is
    # long enough to filter transient network noise and short enough that a valuable
    # position is not blind for hours.

    EXIT_FAIL_ALERT_STREAK = 5

    @staticmethod
    def _exit_fail_key(peak_key: Optional[str], position_config: dict) -> str:
        return peak_key or monitor.safe_str(position_config.get("market_slug")) or "unknown_position"

    def _note_exit_failure(self, peak_key: Optional[str], position_config: dict,
                           exc: BaseException) -> int:
        """Record one failure and escalate when the streak reaches the threshold.
        Returns the current consecutive failure count."""
        key = self._exit_fail_key(peak_key, position_config)
        streak = self._exit_fail_streak.get(key, 0) + 1
        self._exit_fail_streak[key] = streak
        if streak >= self.EXIT_FAIL_ALERT_STREAK and key not in self._exit_fail_notified:
            slug = monitor.safe_str(position_config.get("market_slug")) or key
            # Latch on the **send result**, not unconditionally. Latching on the
            # attempt would mean a failed send is never retried for this outage —
            # the same shape as the HALT alert above.
            if _admin_telegram_notify(
                f"⚠️ Exit evaluation has failed {streak} times in a row — this position "
                f"cannot currently decide whether to sell."
                f"\nPosition: {slug[:80]}"
                f"\nError: {type(exc).__name__}: {str(exc)[:200]}"
                f"\nOther positions are unaffected. A recovery message will follow."
            ):
                self._exit_fail_notified.add(key)
        return streak

    def _note_exit_recovered(self, peak_key: Optional[str], position_config: dict) -> None:
        key = self._exit_fail_key(peak_key, position_config)
        streak = self._exit_fail_streak.pop(key, 0)
        if key in self._exit_fail_notified:
            self._exit_fail_notified.discard(key)
            slug = monitor.safe_str(position_config.get("market_slug")) or key
            _admin_telegram_notify(
                f"✅ Exit evaluation recovered — {slug[:80]} (after {streak} failures)")

    def tick(self) -> dict:
        gate = self.live_gate.resolve()
        kill = self.kill_switch.status()
        effective_mode = gate["effective_mode"]
        # Sync the CapGuard ceiling from the owner arm-state effective caps so
        # the owner's frontend-set caps take effect this tick. None (no cap field in
        # the arm-state) leaves the config-derived caps untouched. Only raises/
        # lowers HER single-tenant ceiling (multi-tenant users are a separate
        # process); deployed_usd is tracked independently and unaffected.
        eff_total = gate.get("effective_max_total_deploy_usd")
        eff_per_trade = gate.get("effective_max_per_trade_usd")
        if eff_total is not None:
            self.cap_guard.total_cap_usd = float(eff_total)
        if eff_per_trade is not None:
            self.cap_guard.per_trade_cap_usd = float(eff_per_trade)
        # An explicit epoch reopen upstream resets the local advisory counter so it
        # agrees with the authoritative basis.
        if self.cap_guard.sync_epoch(gate.get("budget_epoch")):
            self.alerter.alert("budget_epoch_reset", {
                "budget_epoch": gate.get("budget_epoch"),
                "caps": self.cap_guard.status(),
            })
        # The advisory counter resets each tick, which gates repeated buying inside
        # one tick. Budget across ticks belongs to the authoritative gate — a net
        # exposure cap plus a drawdown fuse, fail-closed against real account state
        # — rechecked before every live BUY.
        self.cap_guard.reset_tick_window()
        record: dict = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_now(),
            "tick_id": f"atd_{now_ms()}",
            "daemon_mode": gate.get("arm_mode", "off"),
            "boundaries": BOUNDARIES,
            "live_gate": gate,
            "kill_switch": kill,
            "caps": self.cap_guard.status(),
            "executor": self.executor.name,
            "win_rate_provider": self.win_provider.name,
            "pending_intents": _pending_intents_summary(),
            "positions": [],
            "entries": [],
            "position_discovery": {"status": "not_started"},
        }
        if kill["active"]:
            record["halt"] = {"reason": "kill switch active; no execution this tick", "source": kill}
            self.alerter.alert("kill_switch_active", kill)
            return record
        if kill.get("global_halt_active"):
            auto_clear = _try_auto_clear_reserve_halt()
            if auto_clear.get("cleared"):
                record["halt"] = {
                    "reason": "reserve-breach HALT auto-cleared this tick; BUY resumes next tick",
                    "auto_clear": auto_clear,
                }
                self.alerter.alert("global_halt_auto_cleared", auto_clear)
                _admin_telegram_notify(
                    "✅ Reserve-breach HALT cleared automatically: the squeeze source is "
                    "gone and no pending intent survives."
                    "\nBUY resumes next tick; every order still passes the budget pre-check."
                    f"\nArchived: {os.path.basename(str(auto_clear.get('archived_to') or ''))}")
                self._halt_tg_notified = False
            else:
                record["halt"] = {
                    "reason": "global HALT active; BUY disabled but SELL exits remain enabled",
                    "source": kill,
                    "auto_clear_check": auto_clear,
                }
                if not getattr(self, "_halt_tg_notified", False):
                    # Latch on the **send result**, not unconditionally. Latching on
                    # the attempt meant a failed send was never retried for the whole
                    # HALT period, until the HALT cleared or the process restarted.
                    # The failure chain reads: HALT engages -> alert lost -> BUY
                    # silently stops for days -> somebody eventually asks why nothing
                    # is trading.
                    #
                    # Hardening the transport is not enough: it lowers the chance of a
                    # failed send without removing the latch. The retry cost is bounded
                    # and stays inside the degraded-liveness threshold.
                    self._halt_tg_notified = _admin_telegram_notify(
                        "🛑 Global HALT is in effect — BUY stopped; SELL and redeem continue."
                        f"\nContent: {str(auto_clear.get('halt_content') or '')[:160]}"
                        f"\nNot self-clearing because: {auto_clear.get('reason')}"
                        "\nA reserve-breach HALT clears once pending intents drain; every "
                        "other kind needs a human.")
        else:
            self._halt_tg_notified = False

        positions_state: Any = POSITION_DISCOVERY_FAILED
        try:
            positions, position_meta = discover_positions_with_meta(
                self.config,
                secret_dir=self.args.secret_dir,
                alerter=self.alerter,
            )
        except Exception as exc:
            payload = exception_payload(exc, source=str(self.config.get("position_source") or "config"))
            record["positions_error"] = str(exc)
            record["position_discovery"] = {
                "status": "failed",
                "positions_state": "unknown",
                "positions_confirmed_count": None,
                "sdk_confirmed_empty": False,
                **payload,
            }
            self.alerter.alert("position_discovery_failed", payload)
        else:
            positions_state = positions
            record["position_discovery"] = {
                "status": "ok",
                "positions_state": "confirmed_empty" if not positions else "confirmed_open",
                **position_meta,
            }

        # Real spendable balance -> sizing's third ceiling (260725). Only read when a
        # BUY could actually route this tick; fail-soft None keeps the cap-only path.
        may_buy = not (kill.get("buy_halted") or kill.get("active"))
        if may_buy and str(self.config.get("position_source") or "") == "live_account":
            self.cap_guard.available_collateral_usd = read_available_collateral_usd(
                secret_dir=self.args.secret_dir,
                timeout_sec=float(self.config.get("balance_read_timeout_sec", 15.0)),
            )
        record["available_collateral_usd"] = self.cap_guard.available_collateral_usd

        if positions_state is POSITION_DISCOVERY_FAILED:
            record["entries_skipped"] = {
                "reason": "position discovery failed; holding this tick before entry/autonomous",
                "positions_state": "unknown",
            }
            record["autonomous_turn"] = {
                **self.autonomous_turn.status(),
                "triggered": False,
                "reason": "position discovery failed; skipping autonomous turn",
            }
            self._load_experience_metrics(record)
            record["caps"] = self.cap_guard.status()
            return record

        exit_arm_mode = gate.get("arm_mode") or "off"
        exit_buy_halted = bool(kill.get("buy_halted"))
        exit_bankroll = self.cap_guard.total_cap_usd
        for position_config in positions_state:
            peak_key = _position_peak_key(position_config)
            if peak_key is not None and peak_key in self._exit_peaks:
                position_config = {**position_config, "prev_peak_price": self._exit_peaks[peak_key]}
            # canary bankroll unlocks target-driven scale-in (favourable add) inside
            # the monitor; arm_mode/buy_halted gate whether that BUY may actually route.
            position_config = {**position_config, "kelly_bankroll_usd": exit_bankroll}
            try:
                pos_result = self.exit_manager.process_position(
                    position_config, effective_mode=effective_mode,
                    arm_mode=exit_arm_mode, buy_halted=exit_buy_halted)
                record["positions"].append(pos_result)
                if peak_key is not None:
                    sell_price = to_float(pos_result.get("break_even_probability"))
                    if sell_price is not None:
                        self._exit_peaks[peak_key] = max(self._exit_peaks.get(peak_key, 0.0), sell_price)
                self._note_exit_recovered(peak_key, position_config)
            except Exception as exc:
                payload = exception_payload(exc, position=position_config)
                streak = self._note_exit_failure(peak_key, position_config, exc)
                payload["consecutive_failures"] = streak
                record["positions"].append({
                    "stage": "exit",
                    "decision": "POSITION_ERROR",
                    "reason": str(exc),
                    "intent": None,
                    "execution": None,
                    "error_type": payload["error_type"],
                    "consecutive_failures": streak,
                })
                self.alerter.alert("position_exit_failed", payload)

        # MarketFlow queued BUY intents run only in full mode. MarketFlow queued SELL
        # reduce/exit intents run in exit_only/full mode; off does not consume
        # them, avoiding dry-run one-shot burn before the owner arms exits.
        arm_mode = gate.get("arm_mode")
        if arm_mode in ("exit_only", "full"):
            try:
                record["entries"] = self.entry_manager.tick(
                    effective_mode=effective_mode,
                    allow_buy=(arm_mode == "full" and not kill.get("buy_halted")),
                    allow_sell=True,
                )
            except Exception as exc:
                payload = exception_payload(exc, arm_mode=arm_mode, effective_mode=effective_mode)
                record["entries_error"] = payload
                record["entries"].append({
                    "stage": "entry",
                    "decision": "ENTRY_SEGMENT_ERROR",
                    "reason": str(exc),
                    "intent": None,
                    "execution": None,
                    "error_type": payload["error_type"],
                })
                self.alerter.alert("entry_segment_failed", payload)

        try:
            trigger, reason = self.autonomous_turn.should_trigger(record)
            if trigger:
                auto = self.autonomous_turn.run(record=record, trigger_reason=reason)
                auto_effective_mode = "dry_run" if self.autonomous_turn.dry_run_only else effective_mode
                auto["execution"] = self._execute_autonomous_decision(
                    auto,
                    effective_mode=auto_effective_mode,
                    arm_mode=str(gate.get("arm_mode") or "off"),
                    positions=positions_state,
                )
                auto["requested_effective_mode"] = effective_mode
                auto["effective_mode"] = auto_effective_mode
                record["autonomous_turn"] = auto
            else:
                record["autonomous_turn"] = {**self.autonomous_turn.status(), "triggered": False, "reason": reason}
        except Exception as exc:
            payload = exception_payload(exc, effective_mode=effective_mode)
            record["autonomous_turn"] = {
                **self.autonomous_turn.status(),
                "triggered": False,
                "status": "error",
                **payload,
            }
            self.alerter.alert("autonomous_turn_failed", payload)

        self._load_experience_metrics(record)
        record["caps"] = self.cap_guard.status()
        return record

    def _execute_autonomous_decision(
        self,
        auto: dict,
        *,
        effective_mode: str,
        arm_mode: str = "off",
        positions: Any = None,
    ) -> dict:
        decision = auto.get("internal change log")
        if not isinstance(decision, dict):
            return {"accepted": False, "executed": False, "reason": "no structured internal change log"}
        token = str(decision.get("decision") or "NONE").upper()
        if token in ("HOLD", "NONE", "OBSERVE_ONLY"):
            return {"accepted": True, "executed": False, "reason": f"decision {token}: no execution"}
        if token == "ENTRY_SIGNAL":
            if effective_mode == "live" and arm_mode != "full":
                return {
                    "accepted": False,
                    "executed": False,
                    "reason": f"live entry blocked because arm_mode={arm_mode}; full required",
                }
            return self._execute_autonomous_entry(decision, effective_mode=effective_mode)
        if token == "SELL_SIGNAL":
            if effective_mode == "live" and arm_mode not in ("exit_only", "full"):
                return {
                    "accepted": False,
                    "executed": False,
                    "reason": f"live sell blocked because arm_mode={arm_mode}; exit_only/full required",
                }
            return self._execute_autonomous_sell(decision, effective_mode=effective_mode, positions=positions)
        return {"accepted": False, "executed": False, "reason": f"unsupported autonomous decision: {token}"}

    def _execute_autonomous_entry(self, decision: dict, *, effective_mode: str) -> dict:
        market = _decision_market(decision)
        intent = _decision_intent(decision)
        candidate = {
            "schema_version": "polymarket-agent-intent-v0.1",
            "candidate_type": "agent_intent",
            "allow_live_entry": True,
            "one_shot": True,
            "source": "autonomous_turn",
            "action": "BUY",
            "order_kind": "market",
            "market_order_type": intent.get("market_order_type") or "FOK",
            "market_id": market.get("market_id"),
            "market_slug": market.get("market_slug") or market.get("label"),
            "token_id": market.get("token_id"),
            "side": market.get("side") or "YES",
            "ask_price": decision.get("entry_price") or intent.get("max_price"),
            "best_bid": decision.get("best_bid") or intent.get("best_bid") or market.get("best_bid"),
            "shares": decision.get("size"),
            "max_spend_usd": intent.get("max_spend_usd"),
            "max_price": intent.get("max_price") or decision.get("entry_price"),
            "max_loss_usd": intent.get("max_loss_usd") or intent.get("max_spend_usd"),
            "order_min_size": intent.get("order_min_size") or intent.get("order_min_size_shares") or market.get("order_min_size"),
            "tick_size": intent.get("tick_size") or market.get("tick_size"),
            "speed_window": bool(intent.get("speed_window") or decision.get("speed_window")),
            "speed_window_candidate": bool(intent.get("speed_window_candidate") or decision.get("speed_window_candidate")),
            "taker_allowed_reason": intent.get("taker_allowed_reason") or decision.get("taker_allowed_reason"),
            "close_time": intent.get("close_time"),
            "reason": decision.get("reason") or "autonomous structured decision",
            "model_probability": decision.get("predicted_prob"),
            "resolution_confirmed_clean": intent.get("resolution_confirmed_clean"),
            "resolution_source": intent.get("resolution_source"),
            "idempotency_key": decision.get("experience_id"),
        }
        rec = self.entry_manager._process_agent_intent(candidate, effective_mode=effective_mode)
        return rec

    def _execute_autonomous_sell(self, decision: dict, *, effective_mode: str, positions: Any = None) -> dict:
        market = _decision_market(decision)
        intent = _decision_intent(decision)
        shares = to_float(decision.get("size"))
        min_price = to_float(intent.get("min_price") or decision.get("entry_price"))
        token_id = monitor.safe_str(market.get("token_id"))
        if shares is None or shares <= 0 or not token_id or min_price is None:
            return {
                "accepted": False,
                "executed": False,
                "reason": "SELL_SIGNAL missing token_id, size, or min_price",
            }
        position_lookup = discovered_position_for_token(positions, token_id)
        if not position_lookup.get("ok"):
            payload = {
                "reason": position_lookup.get("reason"),
                "token_id": token_id,
                "decision_id": monitor.safe_str(decision.get("experience_id")),
                "position_lookup": position_lookup,
            }
            self.alerter.alert("autonomous_sell_position_unavailable", payload)
            return {
                "accepted": False,
                "executed": False,
                "reason": "SELL_SIGNAL refused: current held_shares unavailable for token_id",
                "position_lookup": position_lookup,
            }
        held_shares = float(position_lookup["held_shares"])
        max_loss = proportional_exit_max_loss(
            shares=float(shares),
            held_shares=held_shares,
            min_price=float(min_price),
            position_lookup=position_lookup,
        )
        order_intent = OrderIntent(
            intent_id=monitor.safe_str(decision.get("experience_id") or f"auto_exit_{now_ms()}"),
            action="SELL",
            is_exit=True,
            market_id=monitor.safe_str(market.get("market_id")),
            market_slug=monitor.safe_str(market.get("market_slug") or market.get("label")),
            token_id=token_id,
            side=monitor.safe_str(market.get("side") or "YES"),
            shares=float(shares),
            held_shares=held_shares,
            limit_price=float(min_price),
            est_notional_usd=round(float(shares) * float(min_price), 8),
            max_loss_usd=max_loss,
            reason=monitor.safe_str(decision.get("reason") or "autonomous structured exit decision"),
            order_kind="limit",
            min_price=float(min_price),
            idempotency_key=monitor.safe_str(decision.get("experience_id")),
            settlement_cycle=monitor.safe_str(intent.get("settlement_cycle") or intent.get("close_time")),
        )
        result = self.executor.execute(order_intent, effective_mode=effective_mode)
        out = asdict(result)
        out["position_lookup"] = position_lookup
        return out

    def _emit(self, record: dict) -> None:
        append_jsonl_rotating(self.args.ledger, record)
        write_json(self.args.latest_json, record)
        ensure_parent(self.args.latest_summary)
        with open(self.args.latest_summary, "w", encoding="utf-8") as f:
            f.write(human_summary(record))
        if self.args.print_json:
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        else:
            print(one_line_status(record))

    def _failure_record(self, payload: dict) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_now(),
            "tick_id": f"atd_error_{now_ms()}",
            "daemon_mode": "unknown",
            "boundaries": BOUNDARIES,
            "live_gate": {},
            "kill_switch": {},
            "caps": self.cap_guard.status(),
            "executor": self.executor.name,
            "win_rate_provider": self.win_provider.name,
            "positions": [],
            "entries": [],
            "position_discovery": {"status": "unknown"},
            "daemon_loop_error": payload,
        }

    def _handle_loop_exception(self, exc: BaseException, *, record: Optional[dict]) -> dict:
        payload = exception_payload(exc, stage="tick_emit_loop")
        print(f"daemon tick failed: {payload['error_type']}: {payload['error']}", file=sys.stderr)
        self.alerter.alert("daemon_tick_failed", payload)
        if record is not None:
            record["daemon_loop_error"] = payload
            return record
        failure = self._failure_record(payload)
        try:
            self._emit(failure)
        except Exception as emit_exc:
            emit_payload = exception_payload(emit_exc, stage="tick_failure_emit")
            print(f"daemon failure emit failed: {emit_payload['error_type']}: {emit_payload['error']}", file=sys.stderr)
            self.alerter.alert("daemon_failure_emit_failed", emit_payload)
        return failure

    def run(self) -> int:
        self.instance_lock.acquire()
        self.kill_switch.install_signal_handlers()
        try:
            interval = float(self.config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
            ticks_done = 0
            last_record: Optional[dict] = None
            while True:
                record: Optional[dict] = None
                try:
                    record = self.tick()
                    self._emit(record)
                    last_record = record
                except Exception as exc:
                    last_record = self._handle_loop_exception(exc, record=record)
                ticks_done += 1

                # File-based kill (front-end emergency stop) must NOT exit the
                # process: the per-tick kill_switch.active() checks already block
                # every execution, so the daemon stays resident in dry_run. Exiting
                # on a file kill would fight launchd KeepAlive into a ~10s respawn
                # loop. Only an OS signal (SIGINT/SIGTERM), handled after the sleep
                # below, exits the loop.
                if self.args.once:
                    break
                if self.args.ticks > 0 and ticks_done >= self.args.ticks:
                    break
                slept = 0.0
                while slept < interval:
                    # Only an OS signal cuts the sleep short (for prompt exit). A
                    # file kill must NOT shorten the sleep, or the resident dry_run
                    # loop would spin without sleeping (busy loop / 100% CPU).
                    if self.kill_switch.signalled():
                        break
                    step = min(0.5, interval - slept)
                    time.sleep(step)
                    slept += step
                if self.kill_switch.signalled():
                    final: Optional[dict] = None
                    try:
                        final = self.tick()
                        self._emit(final)
                        last_record = final
                    except Exception as exc:
                        last_record = self._handle_loop_exception(exc, record=final)
                    break

            if last_record and last_record.get("kill_switch", {}).get("active"):
                return 3
            return 0
        finally:
            self.instance_lock.release()


def one_line_status(record: dict) -> str:
    gate = record.get("live_gate", {})
    positions = record.get("positions", [])
    entries = record.get("entries", [])
    decisions = [p.get("decision") for p in positions]
    entry_decisions = [e.get("decision") for e in entries]
    return (
        f"{record.get('generated_at')} mode={gate.get('effective_mode')} "
        f"positions={len(positions)} decisions={decisions} "
        f"entries={len(entries)} entry_decisions={entry_decisions} "
        f"kill={record.get('kill_switch', {}).get('active')}"
    )


def human_summary(record: dict) -> str:
    gate = record.get("live_gate", {})
    kill = record.get("kill_switch", {})
    caps = record.get("caps", {})
    positions = record.get("positions", [])
    auto = record.get("autonomous_turn") or {}
    metrics = record.get("experience_metrics") or {}
    lines = [
        "# Autonomous loop daemon",
        "",
        f"- generated: `{record.get('generated_at')}`",
        f"- mode: `{record.get('daemon_mode')}` (off = all dry run / exit_only = sell only / full = sell and open)",
        f"- effective mode: `{gate.get('effective_mode')}` ({gate.get('reason')})",
        f"- armed: `{gate.get('armed')}`",
        f"- kill switch: `{kill.get('active')}` (file={kill.get('file_active')} signal={kill.get('signalled')})",
        f"- executor: `{record.get('executor')}` (dry_run_stub = places nothing / s1 = through the execution fuses)",
        f"- win-rate provider: `{record.get('win_rate_provider')}`",
        f"- caps: total `{caps.get('total_cap_usd')}` / per-trade `{caps.get('per_trade_cap_usd')}` / deployed `{caps.get('deployed_usd')}` (the authoritative fuses live in the execution module)",
        f"- autonomous turn: `{auto.get('status') or auto.get('reason')}` (trigger={auto.get('trigger_reason')})",
        f"- paper NAV: `{metrics.get('snowball_nav_usd')}` / win rate `{metrics.get('win_rate')}` / Brier `{metrics.get('brier_score')}`",
        "",
        "## Positions (exit-first)",
        "",
    ]
    if record.get("halt"):
        lines.append(f"- halted: {record['halt'].get('reason')}")
    if record.get("positions_error"):
        lines.append(f"- position discovery error: {record['positions_error']}")
    if not positions and (record.get("position_discovery") or {}).get("status") == "failed":
        lines.append("- position state unknown; entry and the autonomous turn were skipped this tick")
    elif not positions:
        lines.append("- no managed positions")
    for idx, p in enumerate(positions, start=1):
        wr = p.get("win_rate", {})
        ex = p.get("execution")
        lines.extend([
            f"### {idx}. {p.get('market')}",
            "",
            f"- decision: `{p.get('decision')}` ({p.get('decision_reason') or p.get('reason')})",
            f"- win probability: `{wr.get('win_probability')}` (source {wr.get('source')})",
            f"- break-even probability for selling now: `{p.get('break_even_probability')}`",
            f"- immediate exit value: `{p.get('immediate_exit_value')}`",
            f"- execution: `{(ex or {}).get('executed')}` ({(ex or {}).get('reason') if ex else 'no action'})",
            "",
        ])
    if auto.get("internal change log"):
        dec = auto.get("internal change log") or {}
        ex = auto.get("execution") or {}
        lines.extend([
            "## Autonomous turn",
            "",
            f"- turn_id: `{auto.get('turn_id')}`",
            f"- decision: `{dec.get('decision')}`",
            f"- reason: {dec.get('reason')}",
            f"- research summary: {dec.get('research_summary')}",
            f"- execution: `{ex.get('executed')}` ({ex.get('reason') or ex.get('decision')})",
            "",
        ])
    lines.extend([
        "## Boundaries",
        "",
        "Dry run by default. Without an armed arm-state nothing is ever placed, and "
        "secrets are never printed.",
        "Live execution goes only through the execution module's authoritative fuses. "
        "An explicit kill stops everything; a HALT stops BUY only and never blocks an "
        "exit.",
        "",
    ])
    return "\n".join(lines)


def _assert(checks: dict, key: str, value: bool) -> None:
    checks[key] = bool(value)


def selftest() -> dict:
    # The self-test must not reach the venue. Replace the one network seam with a
    # stub that returns nothing, which drives _market_fee_rate down its conservative
    # fallback, and count the calls so an offline run proves it stayed offline.
    global _FEE_RATE_FETCH, _BOOK_FETCH
    saved_fee_fetch, saved_book_fetch = _FEE_RATE_FETCH, _BOOK_FETCH
    fee_fetch_calls: list[str] = []
    book_fetch_calls: list[str] = []

    def _stub_fee_fetch(url: str, timeout: float = 5.0) -> list:
        fee_fetch_calls.append(url)
        return []

    def _stub_book_fetch(token_id: str) -> dict:
        book_fetch_calls.append(token_id)
        return {"bids": [], "asks": []}

    _FEE_RATE_FETCH, _BOOK_FETCH = _stub_fee_fetch, _stub_book_fetch
    try:
        report = _selftest_body(fee_fetch_calls, book_fetch_calls)
    finally:
        _FEE_RATE_FETCH, _BOOK_FETCH = saved_fee_fetch, saved_book_fetch
    return report


def _selftest_body(fee_fetch_calls: list, book_fetch_calls: list) -> dict:
    saved_global_halt = pmx.DEFAULT_GLOBAL_HALT_FILE
    tmp_global_halt = os.path.join(OUT_DIR, "selftest_GLOBAL_HALT_DOES_NOT_EXIST")
    pmx.DEFAULT_GLOBAL_HALT_FILE = tmp_global_halt
    if os.path.exists(tmp_global_halt):
        os.remove(tmp_global_halt)
    checks: dict = {}

    gate_default = LiveGate(requested_live=False, arm_state_file="/no/such/arm.json").resolve()
    _assert(checks, "default_is_dry_run", gate_default["effective_mode"] == "dry_run")

    gate_live_no_arm = LiveGate(requested_live=True, arm_state_file="/no/such/arm.json").resolve()
    _assert(checks, "live_without_arm_falls_back_dry_run", gate_live_no_arm["effective_mode"] == "dry_run")

    arm_path = os.path.join(OUT_DIR, "selftest_arm_state.json")
    ensure_parent(arm_path)

    def _write_arm(*, armed=True, mode="full", writer=pmx.ARM_WRITER):
        write_json(arm_path, {
            "schema_version": pmx.ARM_STATE_SCHEMA_VERSION,
            "armed": armed, "mode": mode,
            "live_ack": pmx.LIVE_ACK_PHRASE, "written_by": writer,
            "budget_epoch": "selftest-epoch",
            "budget_epoch_started_at": "2026-06-19T00:00:00Z",
        })

    try:
        _write_arm(mode="full")
        gate_full = LiveGate(requested_live=True, arm_state_file=arm_path).resolve()
        _assert(checks, "armed_full_unlocks_live", gate_full["effective_mode"] == "live" and gate_full["arm_mode"] == "full")

        _write_arm(mode="exit_only")
        gate_exit = LiveGate(requested_live=True, arm_state_file=arm_path).resolve()
        _assert(checks, "armed_exit_only_is_live_exit_mode", gate_exit["effective_mode"] == "live" and gate_exit["arm_mode"] == "exit_only")

        _write_arm(armed=False, mode="off")
        gate_off = LiveGate(requested_live=True, arm_state_file=arm_path).resolve()
        _assert(checks, "disarmed_stays_dry_run", gate_off["effective_mode"] == "dry_run")

        _write_arm(mode="full", writer="marketflow")
        gate_selfarm = LiveGate(requested_live=True, arm_state_file=arm_path).resolve()
        _assert(checks, "self_armed_stays_dry_run", gate_selfarm["effective_mode"] == "dry_run")
    finally:
        if os.path.exists(arm_path):
            os.remove(arm_path)

    # Owner daemon path: the S1 adapter builds caps via owner_fuse_caps, so the owner's
    # own daemon CAN raise its caps above the default weld — up to the owner
    # fat-finger ceiling; a stray extra zero past the ceiling still clamps down.
    hi = S1ExecutionAdapter({"max_total_deploy_usd": 500.0, "max_per_trade_usd": 50.0})
    hi_d = hi._resolve(pmx)
    hi_caps = pmx.owner_fuse_caps(
        max_total_deploy_usd=hi_d["max_total_deploy_usd"],
        max_per_trade_usd=hi_d["max_per_trade_usd"],
    )
    _assert(checks, "owner_daemon_can_raise_caps_to_ceiling",
            hi_caps.max_total_deploy_usd == 500.0 and hi_caps.max_per_trade_usd == 50.0)
    fat = S1ExecutionAdapter({"max_total_deploy_usd": 1.0e9, "max_per_trade_usd": 1.0e9})
    fat_d = fat._resolve(pmx)
    fat_caps = pmx.owner_fuse_caps(
        max_total_deploy_usd=fat_d["max_total_deploy_usd"],
        max_per_trade_usd=fat_d["max_per_trade_usd"],
    )
    _assert(checks, "owner_daemon_caps_clamp_at_fat_finger_ceiling",
            fat_caps.max_total_deploy_usd == pmx.OWNER_CAP_CEILING_TOTAL_USD
            and fat_caps.max_per_trade_usd == pmx.OWNER_CAP_CEILING_PER_TRADE_USD)

    stub_exec = DryRunStubExecutionAdapter()
    dummy_intent = OrderIntent(
        intent_id="t", action="SELL", is_exit=True, market_id="m", market_slug="s", token_id="tok",
        side="YES", shares=10.0, limit_price=0.5, est_notional_usd=5.0, max_loss_usd=0.0, reason="t",
        held_shares=10.0,
    )
    dry_res = stub_exec.execute(dummy_intent, effective_mode="dry_run")
    _assert(checks, "dry_run_stub_executes_nothing", dry_res.executed is False and dry_res.simulated is True)
    live_res = stub_exec.execute(dummy_intent, effective_mode="live")
    _assert(checks, "live_stub_failsafe_no_execution", live_res.executed is False and live_res.accepted is False)

    cap = CapGuard(total_cap_usd=DEFAULT_TOTAL_CAP_USD, per_trade_cap_usd=DEFAULT_PER_TRADE_CAP_USD)
    _assert(checks, "cap_allows_within_limits", cap.check_entry(DEFAULT_PER_TRADE_CAP_USD * 0.75)["allowed"] is True)
    _assert(checks, "cap_blocks_over_per_trade", cap.check_entry(DEFAULT_PER_TRADE_CAP_USD * 1.5)["allowed"] is False)
    cap.record_entry(DEFAULT_PER_TRADE_CAP_USD * 0.5)
    cap.record_entry(DEFAULT_PER_TRADE_CAP_USD * 0.5)
    _assert(checks, "cap_tracks_deployed", abs(cap.deployed_usd - DEFAULT_PER_TRADE_CAP_USD) < 1e-9)
    _floor_hi = DEFAULT_PER_TRADE_CAP_USD * 2.4
    _floor_lo = DEFAULT_PER_TRADE_CAP_USD * 0.8
    cap.sync_deployed_floor(_floor_hi)
    cap.sync_deployed_floor(_floor_lo)
    _assert(checks, "cap_sync_deployed_floor_only_raises", abs(cap.deployed_usd - _floor_hi) < 1e-9)

    # 260725: real balance is a third sizing ceiling beside per-trade / remaining cap.
    # Without it every order is built at the cap and the exchange rejects it.
    _bal_cap = CapGuard(total_cap_usd=200.0, per_trade_cap_usd=20.0)
    _bal_cand = {"market_prob": 0.84, "order_min_size": 5.0}
    _size_kw = dict(model_probability=0.9176, executable_price=0.84, order_price=0.8484)
    _unknown = fractional_kelly_buy_sizing(_bal_cand, cap_guard=_bal_cap, **_size_kw)
    _bal_cap.available_collateral_usd = 18.66
    _tight = fractional_kelly_buy_sizing(_bal_cand, cap_guard=_bal_cap, **_size_kw)
    _assert(checks, "sizing_unknown_balance_keeps_cap_only_behaviour", _unknown.get("ok") is True)
    _assert(checks, "sizing_clamped_to_available_balance",
            _tight.get("ok") is True
            and _tight["estimated_notional_usd"] <= 18.66 * 0.98 + 1e-6
            and _tight["estimated_notional_usd"] < _unknown["estimated_notional_usd"])
    _bal_cap.available_collateral_usd = 0.0
    _assert(checks, "sizing_zero_balance_blocks_entry",
            fractional_kelly_buy_sizing(_bal_cand, cap_guard=_bal_cap, **_size_kw).get("ok") is False)
    # The Kelly bankroll must be the account's real capital, not the cap total:
    # cap-as-bankroll makes every target hit the per-trade cap, which on an account
    # far smaller than the cap total is close to going all in each time.
    _bk_cap = CapGuard(total_cap_usd=200.0, per_trade_cap_usd=15.0)
    _bk_cap.available_collateral_usd = 100.0
    _real = fractional_kelly_buy_sizing(_bal_cand, cap_guard=_bk_cap, **_size_kw)
    _assert(checks, "sizing_bankroll_uses_real_balance_not_cap_total",
            _real.get("ok") is True and abs(_real["kelly_bankroll_usd"] - 100.0) < 1e-9
            and _real["kelly_target_notional_usd"] < 15.0)
    _override = fractional_kelly_buy_sizing(dict(_bal_cand, kelly_bankroll_usd=50.0),
                                            cap_guard=_bk_cap, **_size_kw)
    _assert(checks, "sizing_bankroll_intent_override_wins",
            _override.get("ok") is True and abs(_override["kelly_bankroll_usd"] - 50.0) < 1e-9)
    # An exchange minimum above a quarter of the real bankroll is refused: the
    # venue's floor must not push a small account into overexposure.
    _bk_tiny = CapGuard(total_cap_usd=200.0, per_trade_cap_usd=20.0)
    _bk_tiny.available_collateral_usd = 10.0
    _tiny = fractional_kelly_buy_sizing(_bal_cand, cap_guard=_bk_tiny, **_size_kw)
    _assert(checks, "sizing_exchange_min_blocked_when_over_quarter_bankroll",
            _tiny.get("ok") is False and "bankroll" in str(_tiny.get("reason", "")))
    # An epoch reopen resets the advisory counter to agree with the authoritative
    # basis. The first epoch seen is recorded only; an unchanged or absent one does
    # nothing.
    _assert(checks, "cap_epoch_first_seen_no_reset",
            cap.sync_epoch("poly-a") is False and abs(cap.deployed_usd - _floor_hi) < 1e-9)
    _assert(checks, "cap_epoch_same_no_reset",
            cap.sync_epoch("poly-a") is False and abs(cap.deployed_usd - _floor_hi) < 1e-9)
    _assert(checks, "cap_epoch_none_no_reset",
            cap.sync_epoch(None) is False and abs(cap.deployed_usd - _floor_hi) < 1e-9)
    _assert(checks, "cap_epoch_change_resets_deployed",
            cap.sync_epoch("poly-b") is True and cap.deployed_usd == 0.0
            and cap.last_budget_epoch == "poly-b")
    # The advisory counter resets each tick, gating repeated buying within a tick;
    # budget across ticks belongs to the authoritative gate.
    cap.record_entry(3.0)
    cap.reset_tick_window()
    _assert(checks, "cap_tick_window_reset_clears_deployed", cap.deployed_usd == 0.0)

    timeout_blocked = False
    try:
        _with_hard_timeout(0.01, "selftest timeout guard", lambda: time.sleep(0.05))
    except DaemonError as exc:
        timeout_blocked = "timed out" in str(exc)
    _assert(checks, "position_discovery_timeout_guard_fires", timeout_blocked)

    # A fuse alert must not honour an env proxy. When a deployment points proxy
    # variables at a venue tunnel for the execution chain, a dead tunnel must still
    # let HALT and reserve-breach alerts out. This check is fully offline.
    def _opener_proxies(op: Any) -> dict:
        """The proxy mapping this opener will actually use; empty means direct.

        It asserts behaviour rather than structure, because the structure is
        counter-intuitive: the handler passed to build_opener(ProxyHandler({}))
        **does not appear in op.handlers**. ProxyHandler attaches a `<scheme>_open`
        method per proxy key, an empty dict attaches none, and add_handler drops it
        as offering nothing. It has already done its job by making build_opener skip
        the default env-reading ProxyHandler, and the net effect is the direct
        connection we want. So this merges the proxies of every ProxyHandler present
        and checks for empty."""
        merged: dict = {}
        for h in op.handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                merged.update(h.proxies)
        return merged

    _saved_proxy_env = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy")}
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:15237"
    os.environ["https_proxy"] = "http://127.0.0.1:15237"
    try:
        _tg_paths = _tg_openers()
        _assert(checks, "tg_direct_opener_is_first_and_ignores_env_proxy",
                _opener_proxies(_tg_paths[0][0]) == {})
        _assert(checks, "tg_fallback_opener_honors_env_proxy",
                _opener_proxies(_tg_paths[1][0]).get("https") == "http://127.0.0.1:15237")
    finally:
        for _k, _v in _saved_proxy_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
    # Stop anybody raising the timeout far enough to stall a tick: the bound is half
    # the shortest interval the daemon runs at.
    _assert(checks, "tg_worst_case_blocking_under_half_canary_tick",
            sum(t for _o, t in _tg_openers()) <= 15.0)
    # A failed alert leaves a trace, and the trace must not leak the token into the
    # service log.
    _fake_tok = "1234567890:AAHfake-secret-abcdef"
    _redacted = _redact_token(
        f"URLError <urlopen error https://api.telegram.org/bot{_fake_tok}/sendMessage>", _fake_tok)
    _assert(checks, "tg_failure_log_redacts_token",
            _fake_tok not in _redacted and "1234567890" not in _redacted
            and "<TG_TOKEN>" in _redacted)
    # Switch routes only on a transport failure. If the server answered, another
    # route gets the same answer, and retrying into a rate limit makes it worse.
    _assert(checks, "tg_transport_error_tries_other_path",
            _tg_should_try_other_transport(urllib.error.URLError("connection refused")) is True)
    _assert(checks, "tg_http_error_does_not_retry_other_path",
            _tg_should_try_other_transport(
                urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)) is False)

    ks = KillSwitch(kill_file="/no/such/kill")
    _assert(checks, "killswitch_inactive_by_default", ks.active() is False)
    ks._signalled = True
    _assert(checks, "killswitch_signal_activates", ks.active() is True)

    lock_path = os.path.join(OUT_DIR, "selftest_daemon.pidlock")
    lock1 = SingleInstanceLock(lock_path)
    lock2 = SingleInstanceLock(lock_path)
    lock_blocked = False
    try:
        lock1.acquire()
        try:
            lock2.acquire()
        except DaemonError:
            lock_blocked = True
    finally:
        lock1.release()
        lock2.release()
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
    _assert(checks, "single_instance_lock_blocks_second_daemon", lock_blocked)

    _assert(checks, "entry_buys_with_edge", entry_should_buy(0.70, 0.50, 0.05, 0.0) is True)
    _assert(checks, "entry_skips_without_edge", entry_should_buy(0.52, 0.50, 0.05, 0.0) is False)
    _assert(checks, "critical_win_rate_math", abs(critical_win_rate_for_buy(0.50, 0.05, 100.0) - 0.56) < 1e-9)

    # --- price-expression gate ------------------------------
    # The verified per-share fee supersedes flat bps and is in the SAME unit as the
    # price, so it adds directly to the break-even probability.
    _assert(checks, "critical_win_rate_takes_per_share_fee",
            abs(critical_win_rate_for_buy(0.50, 0.05, 0.0, fee_per_share=0.0125) - 0.5625) < 1e-9)
    _assert(checks, "per_share_fee_overrides_flat_bps",
            abs(critical_win_rate_for_buy(0.50, 0.05, 100.0, fee_per_share=0.0) - 0.55) < 1e-9)
    # A maker open pays nothing, so making the fee real cannot change the maker
    # path at all — the only behaviour that moves is the in-play taker window.
    _assert(checks, "maker_path_unchanged_by_fee_awareness",
            entry_should_buy(0.60, 0.50, 0.05, 0.0, fee_per_share=0.0)
            == entry_should_buy(0.60, 0.50, 0.05, 0.0))
    _assert(checks, "taker_fee_only_ever_tightens",
            entry_should_buy(0.5600, 0.50, 0.05, 0.0) is True
            and entry_should_buy(0.5600, 0.50, 0.05, 0.0, fee_per_share=0.0125) is False)
    # Why enforcing changes no behaviour, provable rather than merely measured: the
    # price-expression gate refuses when `edge - fee <= 0`, while the entry gate
    # admits only when `edge > edge_buffer + fee`. With a positive edge buffer,
    # everything the first refuses the second already refused, so the first is a
    # proper subset — it just refuses earlier and with a more precise reason code.
    # What follows verifies that containment exhaustively over the whole grid.
    _subset_ok = True
    for _px in (0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99):
        for _mk in (True, False):
            _fee = 0.0 if _mk else mgate.taker_fee_per_share(_px, mgate.FEE_RATE_FALLBACK)
            for _e in (-0.05, -0.001, 0.0, 0.001, 0.012, 0.05, 0.051, 0.20):
                _p = _px + _e
                if not (0.0 < _p < 1.0):
                    continue
                _pxg_rejects = (_e - _fee) <= 0.0
                _edge_gate_passes = entry_should_buy(_p, _px, 0.05, 0.0, fee_per_share=_fee)
                if _pxg_rejects and _edge_gate_passes:
                    _subset_ok = False
    _assert(checks, "price_expression_rejects_are_subset_of_edge_gate", _subset_ok)

    _pxg_cand = {"market_id": "0xNOPE", "token_id": "t", "side": "YES"}
    _em_shadow = EntryManager(win_provider=StubWinRateProvider(), executor=None, cap_guard=None,
                              kill_switch=None, alerter=None, edge_buffer=0.05, fee_bps=0.0,
                              price_expression="shadow")
    _em_enforce = EntryManager(win_provider=StubWinRateProvider(), executor=None, cap_guard=None,
                               kill_switch=None, alerter=None, edge_buffer=0.05, fee_bps=0.0,
                               price_expression="enforce")
    _em_typo = EntryManager(win_provider=StubWinRateProvider(), executor=None, cap_guard=None,
                            kill_switch=None, alerter=None, edge_buffer=0.05, fee_bps=0.0,
                            price_expression="enfroce")
    _assert(checks, "price_expression_modes_distinct",
            _em_shadow.price_expression_enforce is False and _em_enforce.price_expression_enforce is True)
    # A misspelled mode must land in shadow, not enforce: a typo should never
    # silently change live refusal behaviour.
    _assert(checks, "price_expression_typo_fails_to_shadow", _em_typo.price_expression_enforce is False)
    # The default is enforce, which has to be verified by omitting the argument
    # rather than by the three explicit instances above.
    _em_default = EntryManager(win_provider=StubWinRateProvider(), executor=None, cap_guard=None,
                               kill_switch=None, alerter=None, edge_buffer=0.05, fee_bps=0.0)
    _assert(checks, "price_expression_default_is_enforce", _em_default.price_expression_enforce is True)

    _rec_s: dict = {}
    _g_s = _em_shadow._price_expression(dict(_pxg_cand, price_floor_override_reason=None),
                                        entry_price=0.06, model_probability=0.9, record=_rec_s)
    _rec_e: dict = {}
    _g_e = _em_enforce._price_expression(dict(_pxg_cand), entry_price=0.06,
                                         model_probability=0.9, record=_rec_e)
    _assert(checks, "price_expression_audited_on_every_entry",
            _rec_s["price_expression"] is _g_s and _rec_e["price_expression"] is _g_e)
    _assert(checks, "price_expression_shadow_approves_but_records_verdict",
            _g_s["authorization"] == "APPROVED" and _g_s["would_reject_codes"] == ["BUY_PRICE_FLOOR_REJECT"])
    _assert(checks, "price_expression_enforce_rejects_sub10c",
            _g_e["authorization"] == "REJECTED" and "BUY_PRICE_FLOOR_REJECT" in _g_e["reject_reason_codes"])
    _rec_o: dict = {}
    _g_o = _em_enforce._price_expression(dict(_pxg_cand, price_floor_override_reason="owner: hedge leg"),
                                         entry_price=0.06, model_probability=0.9, record=_rec_o)
    _assert(checks, "price_expression_override_admits_with_reason",
            _g_o["authorization"] == "APPROVED" and _g_o["floor_override_reason"] == "owner: hedge leg")
    # An unreachable Gamma must fall back conservatively, never loosen the gate.
    _assert(checks, "fee_rate_fails_soft_to_fallback",
            _g_e["fee_rate"] == mgate.FEE_RATE_FALLBACK and _g_e["fee_rate_source"] == "fallback")
    _assert(checks, "fee_rate_prefers_candidate_packet",
            _market_fee_rate("0xNOPE", {"fee_rate": 0.03}) == (0.03, "candidate_packet"))
    # A post-only open is a maker: the fee term must be zero, and the taker cost is
    # still reported so the audit shows what crossing would have cost.
    _rec_m: dict = {}
    _g_m = _em_enforce._price_expression(dict(_pxg_cand, fee_rate=0.05), entry_price=0.50,
                                         model_probability=0.60, record=_rec_m)
    _assert(checks, "maker_entry_pays_zero_in_gate",
            _g_m["maker"] is True and _g_m["fee_per_share"] == 0.0
            and abs(_g_m["taker_fee_per_share_if_crossed"] - 0.0125) < 1e-9)
    _rec_t: dict = {}
    _g_t = _em_enforce._price_expression(
        dict(_pxg_cand, fee_rate=0.05, taker_allowed_reason=SPEED_WINDOW_TAKER_REASON),
        entry_price=0.50, model_probability=0.60, record=_rec_t)
    _assert(checks, "speed_window_entry_pays_taker_fee_in_gate",
            _g_t["maker"] is False and abs(_g_t["fee_per_share"] - 0.0125) < 1e-9)

    win_stub = StubWinRateProvider()
    _assert(checks, "winrate_none_when_no_override", win_stub.get_win_probability({}).win_probability is None)
    _assert(checks, "winrate_uses_override", win_stub.get_win_probability({"probability_override": 0.6}).win_probability == 0.6)

    sell_tick = _fake_tick(decision="SELL_SIGNAL", break_even=0.62, win_p=0.40)
    em = ExitManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: sell_tick,
    )
    sell_rec = em.process_position({"probability_override": 0.40, "shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "exit_sell_signal_builds_intent", sell_rec["intent"] is not None and sell_rec["intent"]["action"] == "SELL")
    _assert(checks, "exit_sell_dry_run_no_execution", sell_rec["execution"]["executed"] is False)

    hold_tick = _fake_tick(decision="HOLD", break_even=0.40, win_p=0.70)
    em_hold = ExitManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: hold_tick,
    )
    hold_rec = em_hold.process_position({"probability_override": 0.70, "shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "exit_hold_no_intent", hold_rec["intent"] is None and hold_rec["execution"] is None)

    # dynamic take-profit: a TRIM decision books a PARTIAL sell (trim_shares of held)
    trim_tick = _fake_tick(
        decision="TRIM_SELL_SIGNAL", break_even=0.82, win_p=0.88,
        decision_extra={"trim_shares": 8.0, "keep_fraction": 0.2},
    )
    trim_rec = ExitManager(
        win_provider=StubWinRateProvider(), executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"), alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03, value_fn=lambda cfg: trim_tick,
    ).process_position({"probability_override": 0.88, "shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "exit_trim_builds_partial_sell",
            trim_rec["intent"] is not None and trim_rec["intent"]["action"] == "SELL"
            and trim_rec["intent"]["shares"] == 8.0 and trim_rec["intent"]["held_shares"] == 10.0)
    _assert(checks, "exit_trim_dry_run_no_execution", trim_rec["execution"]["executed"] is False)

    # Repeated exit-evaluation failures must wake somebody. Per-position isolation
    # already existed; escalation is what was missing. This bypasses __init__ and
    # sets only the fields this path needs: what is under test is the latch, not
    # daemon assembly.
    fail_daemon = AutotradeDaemon.__new__(AutotradeDaemon)
    fail_daemon._exit_fail_streak = {}
    fail_daemon._exit_fail_notified = set()
    sent: list[str] = []
    real_notify = globals()["_admin_telegram_notify"]
    globals()["_admin_telegram_notify"] = lambda text: (sent.append(text), True)[1]
    try:
        cfg = {"market_slug": "some-market", "market_id": "mid", "token_id": "tid"}
        streaks = [fail_daemon._note_exit_failure("mid:tid", cfg, RuntimeError("ESPN down"))
                   for _ in range(AutotradeDaemon.EXIT_FAIL_ALERT_STREAK)]
        _assert(checks, "exit_fail_streak_counts_up",
                streaks == list(range(1, AutotradeDaemon.EXIT_FAIL_ALERT_STREAK + 1)))
        _assert(checks, "exit_fail_alerts_only_at_threshold", len(sent) == 1)
        fail_daemon._note_exit_failure("mid:tid", cfg, RuntimeError("ESPN down"))
        _assert(checks, "exit_fail_alert_not_repeated_while_broken", len(sent) == 1)
        fail_daemon._note_exit_recovered("mid:tid", cfg)
        _assert(checks, "exit_fail_recovery_alerts_once", len(sent) == 2 and "recovered" in sent[1])
        _assert(checks, "exit_fail_streak_cleared_on_recovery",
                fail_daemon._exit_fail_streak == {} and not fail_daemon._exit_fail_notified)
        # A failed send must not latch: otherwise this outage is never retried,
        # which is the same shape the HALT alert already hit.
        globals()["_admin_telegram_notify"] = lambda text: False
        for _ in range(AutotradeDaemon.EXIT_FAIL_ALERT_STREAK):
            fail_daemon._note_exit_failure("mid:tid", cfg, RuntimeError("ESPN down"))
        _assert(checks, "exit_fail_send_failure_leaves_latch_open",
                not fail_daemon._exit_fail_notified)
        # one position's failures do not affect another's count
        fail_daemon._note_exit_failure("other:pos", {"market_slug": "other"}, RuntimeError("x"))
        _assert(checks, "exit_fail_streak_is_per_position",
                fail_daemon._exit_fail_streak.get("other:pos") == 1)
    finally:
        globals()["_admin_telegram_notify"] = real_notify
    # a trim that resolves to zero shares falls back to HOLD (no intent, no execution)
    trim_zero_tick = _fake_tick(
        decision="TRIM_SELL_SIGNAL", break_even=0.82, win_p=0.88, decision_extra={"trim_shares": 0.0},
    )
    trim_zero_rec = ExitManager(
        win_provider=StubWinRateProvider(), executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"), alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03, value_fn=lambda cfg: trim_zero_tick,
    ).process_position({"probability_override": 0.88, "shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "exit_trim_zero_falls_back_hold", trim_zero_rec["intent"] is None and trim_zero_rec["decision"] == "HOLD")

    ks_on = KillSwitch(kill_file="/no/such/kill")
    ks_on._signalled = True
    em_kill = ExitManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=ks_on,
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: _fake_tick(decision="SELL_SIGNAL", break_even=0.62, win_p=0.40),
    )
    kill_rec = em_kill.process_position({"probability_override": 0.40, "shares": 10.0}, effective_mode="live")
    _assert(checks, "killswitch_blocks_exit_execution", kill_rec["execution"]["executed"] is False and "kill" in kill_rec["execution"]["reason"].lower())

    blocked_em = ExitManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: (_ for _ in ()).throw(monitor.MonitorError("synthetic orderbook failure")),
    )
    blocked_rec = blocked_em.process_position({"shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "exit_market_data_failure_fail_loud", blocked_rec["decision"] == "MARKET_DATA_BLOCKED" and blocked_rec["intent"] is None)

    cfg_bad = False
    try:
        load_daemon_config_from_obj({"positions": [{"api_secret": "x"}]})
    except monitor.MonitorError:
        cfg_bad = True
    _assert(checks, "sensitive_config_rejected", cfg_bad)

    s1_tmp_ledger = os.path.join(OUT_DIR, "selftest_s1_ledger_absent.jsonl")
    if os.path.exists(s1_tmp_ledger):
        os.remove(s1_tmp_ledger)
    s1 = S1ExecutionAdapter({
        "kill_file": "/no/such/kill",
        "arm_state_file": "/no/such/arm.json",
        "ledger_path": s1_tmp_ledger,
        "estimate_fill": False,
    })
    s1_intent = OrderIntent(
        intent_id="t", action="SELL", is_exit=True, market_id="0xm", market_slug="s",
        token_id="123", side="YES", shares=10.0, limit_price=0.55,
        est_notional_usd=5.5, max_loss_usd=0.0, reason="selftest", held_shares=10.0,
    )
    s1_dry = s1.execute(s1_intent, effective_mode="dry_run")
    _assert(checks, "s1_adapter_dry_run_no_execution", s1_dry.executed is False and s1_dry.simulated is True)
    _assert(checks, "s1_adapter_routes_through_s1_plan", "DRY_RUN_PLAN" in (s1_dry.reason or ""))
    s1_live_no_ack = s1.execute(s1_intent, effective_mode="live")
    _assert(checks, "s1_adapter_live_without_s1_ack_no_execution", s1_live_no_ack.executed is False)
    _assert(checks, "s1_adapter_no_ledger_write_in_dry_run", not os.path.exists(s1_tmp_ledger))

    class _NonEdgeProvider(WinRateProvider):
        name = "non_edge"

        def get_win_probability(self, position_ctx: dict) -> WinRateResult:
            return WinRateResult(
                win_probability=0.70,
                source="fixture",
                as_of=iso_now(),
                confidence="pre_match_market",
                source_class="NON_EDGE_REFERENCE",
                reject_reason_codes=["NON_EDGE_REFERENCE"],
            )

    # Recalibrated: a non-edge in-play estimate now DRIVES the break-even exit
    # (no longer force-NONE). source_class is recorded as metadata, not a veto.
    non_edge_exit = ExitManager(
        win_provider=_NonEdgeProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: _fake_tick(decision="SELL_SIGNAL", break_even=0.62, win_p=0.40),
    ).process_position({"shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "non_edge_reference_allows_exit",
            non_edge_exit["decision"] == "SELL_SIGNAL"
            and non_edge_exit["execution"] is not None
            and non_edge_exit.get("win_rate", {}).get("source_class") == "NON_EDGE_REFERENCE")

    # Truly-blind position (no usable estimate): hold, but alert loudly — never
    # silent OBSERVE_ONLY (the asleep-and-blind failure mode).
    class _BlindProvider(WinRateProvider):
        name = "blind"

        def get_win_probability(self, position_ctx: dict) -> WinRateResult:
            return WinRateResult(
                win_probability=None,
                source="blind",
                as_of=iso_now(),
                confidence="data_unavailable",
                source_class="NO_MODEL_PROBABILITY",
                reject_reason_codes=["MISSING_P_YES_MODEL"],
            )

    blind_alert_file = os.path.join(OUT_DIR, "selftest_blind_alerts.jsonl")
    if os.path.exists(blind_alert_file):
        os.remove(blind_alert_file)
    blind_exit = ExitManager(
        win_provider=_BlindProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=True, alert_file=blind_alert_file),
        risk_buffer=0.03,
        value_fn=lambda cfg: _fake_tick(decision="OBSERVE_ONLY", break_even=0.62, win_p=0.40),
    ).process_position({"shares": 10.0}, effective_mode="dry_run")
    _assert(checks, "blind_exit_holds_no_execution",
            blind_exit["decision"] == "OBSERVE_ONLY" and blind_exit["execution"] is None)
    _assert(checks, "blind_exit_alerts_loud",
            os.path.exists(blind_alert_file)
            and "exit_blind_no_estimate" in open(blind_alert_file, encoding="utf-8").read())

    claim_exit = ExitManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        risk_buffer=0.03,
        value_fn=lambda cfg: {
            "market": {"market_id": "0xm", "slug": "settled-market"},
            "token": {"token_id": "settled-token"},
            "position": {"side": "YES", "shares": 2.0, "entry_cost": 1.0},
            "valuation": {
                "immediate_exit_value": None,
                "break_even_probability": None,
                "claimable_value_usd": 2.0,
                "settlement": {"resolved": True, "redeemable": True, "claimable_value_usd": 2.0},
            },
            "decision": {"decision": "CLAIM_REDEEM_SIGNAL", "reason": "selftest resolved market"},
        },
    ).process_position({"shares": 2.0}, effective_mode="dry_run")
    _assert(checks, "claim_redeem_signal_records_read_only_intent",
            claim_exit["decision"] == "CLAIM_REDEEM_SIGNAL"
            and (claim_exit.get("intent") or {}).get("action") == "CLAIM_REDEEM"
            and (claim_exit.get("execution") or {}).get("executed") is False)

    trigger_client = AutonomousTurnClient({"enabled": True, "trigger_on_tick": True, "min_interval_sec": 0})
    trigger, reason = trigger_client.should_trigger({"positions": [{"decision": "SELL_SIGNAL"}]})
    _assert(checks, "sell_signal_does_not_trigger_autonomous_turn", trigger is False and "mechanically" in reason)
    trigger, reason = trigger_client.should_trigger({"positions": [{"decision": "CLAIM_REDEEM_SIGNAL"}]})
    _assert(checks, "claim_signal_does_not_trigger_autonomous_turn", trigger is False and "mechanically" in reason)
    default_schedule_client = AutonomousTurnClient({"enabled": True, "min_interval_sec": 0})
    trigger, reason = default_schedule_client.should_trigger({"positions": []})
    _assert(checks, "scheduled_autonomous_turn_default_disabled",
            trigger is False and "scheduled autonomous turn disabled" in reason)
    try:
        AutonomousTurnClient({"bridge_url": "https://evil.example/api/chat/autonomous"})
        remote_bridge_rejected = False
    except ValueError:
        remote_bridge_rejected = True
    _assert(checks, "autonomous_turn_remote_bridge_rejected", remote_bridge_rejected)
    _assert(checks, "factory_stub_defaults", make_executor("stub", {}).name == "dry_run_stub" and make_win_provider("stub", {}).name == "stub_probability_override")
    _assert(checks, "factory_real_adapters", make_executor("s1", {}).name == "s1_polymarket_execution")
    _assert(checks, "factory_resolution_verifier_default_on", callable(make_resolution_verifier(default_config())))
    _assert(checks, "factory_resolution_verifier_disabled", make_resolution_verifier({"resolution_verifier": {"enabled": False}}) is None)

    # market-quality gate on the autonomous agent-intent entry path
    def _entry_mgr(cands, market_quality=None, alerter=None, resolution_verifier=None,
                   per_trade_cap=1.0, maker_pricing=DEFAULT_MAKER_PRICING,
                   price_expression=DEFAULT_PRICE_EXPRESSION_MODE):
        return EntryManager(
            win_provider=StubWinRateProvider(),
            executor=DryRunStubExecutionAdapter(),
            cap_guard=CapGuard(25.0, per_trade_cap),
            kill_switch=KillSwitch(kill_file="/no/such/kill"),
            alerter=alerter or Alerter(enabled=False, alert_file="/tmp/none"),
            edge_buffer=0.05, fee_bps=0.0,
            candidate_fn=lambda: [dict(c) for c in cands],
            market_quality=market_quality,
            resolution_verifier=resolution_verifier,
            maker_pricing=maker_pricing,
            price_expression=price_expression,
        )

    _base_intent = {
        "candidate_type": "agent_intent", "allow_live_entry": True, "one_shot": True, "source": "marketflow_chat",
        "action": "BUY", "market_order_type": "FOK", "market_id": "0xm", "token_id": "t",
        "side": "YES", "ask_price": 0.50, "max_price": 0.55, "shares": 2.0,
        "max_spend_usd": 1.0, "max_loss_usd": 1.0,
        "order_min_size": 1.5, "tick_size": 0.01,
        "model_probability": 0.62, "resolution_confirmed_clean": True, "resolution_source": "selftest",
    }
    clean_rec = _entry_mgr([_base_intent]).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_clean_passes_gate", clean_rec["decision"] == "AGENT_INTENT_ENTRY_SIGNAL")
    # End-to-end: the price-expression audit lands on the real entry record, not
    # only when the gate method is called directly — and this manager was built
    # the way the daemon builds it (no explicit mode), so it also pins that the
    # live default really is enforce.
    _assert(checks, "agent_intent_record_carries_price_expression",
            isinstance(clean_rec.get("price_expression"), dict)
            and clean_rec["price_expression"]["fee_rate_effective"] is not None
            and clean_rec["price_expression"]["enforced"] is True)
    # A maker entry must get the same verdict under enforce and under shadow: a
    # post-only order pays no fee, so the fee term cannot bite it. This pins the
    # conclusion that enforcing changes nothing on the main path.
    _shadow_rec = _entry_mgr([_base_intent], price_expression="shadow").tick(effective_mode="dry_run")[0]
    _assert(checks, "maker_entry_same_verdict_shadow_and_enforce",
            _shadow_rec["decision"] == clean_rec["decision"]
            and _shadow_rec["price_expression"]["fee_per_share"] == clean_rec["price_expression"]["fee_per_share"] == 0.0)
    # 260725 live re-pricing: an intent's ask_price is stale by the time it is
    # processed. Unreachable book (selftest token) must fall back to it, not crash.
    _assert(checks, "agent_intent_stale_price_fallback_when_book_unreachable",
            clean_rec.get("ask_price_source") == "intent_stale"
            and abs(float(clean_rec.get("ask_price") or 0) - 0.50) < 1e-9)
    _saved_quote = globals()["_live_book_quote"]
    try:
        globals()["_live_book_quote"] = lambda _t: {"best_bid": 0.44, "best_ask": 0.46, "tick_size": 0.01}
        _repriced = _entry_mgr([_base_intent]).tick(effective_mode="dry_run")[0]
        globals()["_live_book_quote"] = lambda _t: {"best_bid": 0.58, "best_ask": 0.60, "tick_size": 0.01}
        _ran_away = _entry_mgr([_base_intent]).tick(effective_mode="dry_run")[0]
    finally:
        globals()["_live_book_quote"] = _saved_quote
    _assert(checks, "agent_intent_reprices_off_live_book",
            _repriced.get("ask_price_source") == "live_book"
            and abs(float(_repriced.get("ask_price") or 0) - 0.46) < 1e-9
            # maker price now sits on the live bid + tick, so it cannot cross
            and abs(float((_repriced.get("intent") or {}).get("limit_price") or 0) - 0.45) < 1e-9)
    _assert(checks, "agent_intent_refuses_when_price_ran_past_max",
            _ran_away.get("decision") == "NO_ENTRY_PRICE_MOVED_PAST_INTENT_MAX")

    # --- queue-aware maker pricing (fill model), default OFF ---
    # A wide book: bid 0.40 / ask 0.50, i.e. nine empty maker levels in between.
    # Legacy takes the cheapest (0.41, farthest from mid = least fillable);
    # queue-aware takes the most fillable one that still clears the edge ceiling.
    _wide_book = {"best_bid": 0.40, "best_ask": 0.50, "tick_size": 0.01,
                  "bid_levels": [[0.40, 800.0], [0.39, 500.0]], "ask_levels": [[0.50, 600.0]]}
    _wide_cand = dict(_base_intent, ask_price=0.46, max_price=0.55, model_probability=0.62)
    try:
        globals()["_live_book_quote"] = lambda _t: dict(_wide_book)
        _legacy = _entry_mgr([_wide_cand]).tick(effective_mode="dry_run")[0]
        _qa = _entry_mgr([_wide_cand], maker_pricing="queue_aware").tick(effective_mode="dry_run")[0]
        _typo = _entry_mgr([_wide_cand], maker_pricing="queue-aware").tick(effective_mode="dry_run")[0]
        # Empty book on the ask side: queue-aware has no maker/taker boundary and
        # must hand back to the legacy rule rather than refuse an authorised entry.
        globals()["_live_book_quote"] = lambda _t: {"best_bid": 0.40, "best_ask": 0.50,
                                                    "tick_size": 0.01, "bid_levels": [], "ask_levels": []}
        _no_depth = _entry_mgr([_wide_cand], maker_pricing="queue_aware").tick(effective_mode="dry_run")[0]
    finally:
        globals()["_live_book_quote"] = _saved_quote
    _assert(checks, "maker_pricing_defaults_to_legacy_cheapest_level",
            _legacy.get("maker_price", {}).get("basis") == "best_bid_plus_tick"
            and abs(float(_legacy["maker_price"]["price"]) - 0.41) < 1e-9)
    # Ceiling is the LIVE ask (0.50) — the price the edge test just cleared — so the
    # most fillable maker level is one tick under it, eight ticks above legacy's 0.41.
    _assert(checks, "maker_pricing_queue_aware_takes_most_fillable_level",
            _qa.get("maker_price", {}).get("basis") == "queue_aware_max_fillable"
            and abs(float(_qa["maker_price"]["price"]) - 0.49) < 1e-9
            and float(_qa["maker_price"]["queue_ahead"]) == 0.0)
    _assert(checks, "maker_pricing_queue_aware_never_crosses_the_ask",
            float(_qa["maker_price"]["price"]) < 0.50)
    _assert(checks, "maker_pricing_queue_aware_respects_edge_ceiling",
            float(_qa["maker_price"]["price"]) <= float(_qa.get("ask_price") or 0) + 1e-9)
    _assert(checks, "maker_pricing_unknown_value_stays_legacy",
            _typo.get("maker_price", {}).get("basis") == "best_bid_plus_tick")
    _assert(checks, "maker_pricing_queue_aware_falls_back_when_book_unusable",
            _no_depth.get("maker_price", {}).get("basis") == "best_bid_plus_tick"
            and _no_depth.get("decision") == "AGENT_INTENT_ENTRY_SIGNAL")
    clean_intent = clean_rec.get("intent") or {}
    requested_sizing = clean_rec.get("requested_sizing") or {}
    _assert(checks, "agent_intent_uses_kelly_maker_not_requested_size",
            clean_intent.get("order_kind") == "limit"
            and clean_intent.get("post_only") is True
            and abs(float(clean_intent.get("limit_price") or 0.0) - 0.49) < 1e-9
            and abs(float(clean_intent.get("est_notional_usd") or 0.0) - 1.0) < 1e-9
            and abs(float(requested_sizing.get("shares") or 0.0) - 2.0) < 1e-9
            and abs(float(requested_sizing.get("max_spend_usd") or 0.0) - 1.0) < 1e-9)

    ls_rec = _entry_mgr([dict(_base_intent, ask_price=0.08, max_price=0.08, model_probability=0.9)]).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_longshot_rejected",
            ls_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT" and "LONGSHOT_REJECT" in ls_rec.get("reject_reason_codes", []))

    no_model = dict(_base_intent)
    no_model.pop("model_probability")
    nm_rec = _entry_mgr([no_model]).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_no_model_prob_informational",
            nm_rec["decision"] == "NO_ENTRY_NO_EDGE"
            and "NO_MODEL_PROBABILITY" not in (nm_rec.get("quality_gate") or {}).get("reject_reason_codes", []))
    nm_strict_rec = _entry_mgr([no_model], market_quality={"edge_advisory": False}).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_no_model_prob_strict_config_rejected",
            nm_strict_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "NO_MODEL_PROBABILITY" in nm_strict_rec.get("reject_reason_codes", []))

    thin_edge = dict(_base_intent, ask_price=0.55, max_price=0.55, model_probability=0.55)
    thin_rec = _entry_mgr([thin_edge]).tick(effective_mode="dry_run")[0]
    # This tests the quality gate's advisory semantics — a thin edge is recorded
    # rather than hard-refused — not which downstream gate stops it first. With the
    # price-expression gate enforcing, the reason code becomes more specific while
    # the conclusion is unchanged, so either code passes.
    _assert(checks, "agent_intent_low_private_edge_informational",
            thin_rec["decision"] in ("NO_ENTRY_NO_EDGE", "NO_ENTRY_PRICE_EXPRESSION_REJECT")
            and (thin_rec.get("quality_gate") or {}).get("edge_after_cost") is not None
            and "LOW_EDGE_AFTER_COST" not in thin_rec.get("reject_reason_codes", []))
    thin_strict_rec = _entry_mgr([thin_edge], market_quality={"edge_advisory": False}).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_low_private_edge_strict_config_rejected",
            thin_strict_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "LOW_EDGE_AFTER_COST" in thin_strict_rec.get("reject_reason_codes", []))

    shared_key = "selftest_shared_one_shot_key"
    expired_first = dict(_base_intent, idempotency_key=shared_key, expires_at="2000-01-01T00:00:00Z")
    valid_second = dict(_base_intent, idempotency_key=shared_key, expires_at="2999-01-01T00:00:00Z")
    same_key_records = _entry_mgr([expired_first, valid_second]).tick(effective_mode="dry_run")
    _assert(checks, "one_shot_expired_row_does_not_burn_later_same_key",
            same_key_records[0]["decision"] == "NO_ENTRY_AGENT_INTENT_EXPIRED"
            and same_key_records[1]["decision"] == "AGENT_INTENT_ENTRY_SIGNAL")

    class _FailThenAcceptExecutor(ExecutionAdapter):
        name = "fail_then_accept"

        def __init__(self):
            self.calls = 0

        def execute(self, intent: OrderIntent, *, effective_mode: str) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                return ExecutionResult(
                    accepted=False,
                    executed=False,
                    effective_mode=effective_mode,
                    simulated=False,
                    order_id=None,
                    reason="synthetic operational HALT",
                    intent=asdict(intent),
                )
            return ExecutionResult(
                accepted=True,
                executed=False,
                effective_mode=effective_mode,
                simulated=True,
                order_id=None,
                reason="synthetic accepted dry plan",
                intent=asdict(intent),
            )

    retry_exec = _FailThenAcceptExecutor()
    retry_candidate = dict(_base_intent, idempotency_key="selftest_retry_after_halt")
    retry_mgr = EntryManager(
        win_provider=StubWinRateProvider(),
        executor=retry_exec,
        cap_guard=CapGuard(25.0, 1.0),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        edge_buffer=0.05, fee_bps=0.0,
        candidate_fn=lambda: [dict(retry_candidate)],
    )
    retry_first = retry_mgr.tick(effective_mode="live")[0]
    retry_second = retry_mgr.tick(effective_mode="live")[0]
    retry_third = retry_mgr.tick(effective_mode="live")[0]
    _assert(checks, "one_shot_failed_execution_can_retry",
            retry_first["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and (retry_first.get("execution") or {}).get("accepted") is False
            and retry_second["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and (retry_second.get("execution") or {}).get("accepted") is True
            and retry_third["decision"] == "NO_ENTRY_ONE_SHOT_ALREADY_ATTEMPTED")

    class _S1BudgetEchoExecutor(ExecutionAdapter):
        name = "s1_budget_echo"

        def execute(self, intent: OrderIntent, *, effective_mode: str) -> ExecutionResult:
            return ExecutionResult(
                accepted=False,
                executed=False,
                effective_mode=effective_mode,
                simulated=True,
                order_id=None,
                reason="synthetic duplicate idempotency",
                intent={
                    "daemon": asdict(intent),
                    "s1": {
                        "fuses": {
                            "deployed_before_usd": 12.0,
                            "refusals": ["duplicate idempotency key; refusing repeat order for same signal/market/side"],
                        }
                    },
                },
            )

    budget_cap = CapGuard(25.0, 1.0)
    budget_mgr = EntryManager(
        win_provider=StubWinRateProvider(),
        executor=_S1BudgetEchoExecutor(),
        cap_guard=budget_cap,
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        edge_buffer=0.05, fee_bps=0.0,
        candidate_fn=lambda: [dict(_base_intent, idempotency_key="selftest_budget_sync")],
    )
    budget_rec = budget_mgr.tick(effective_mode="live")[0]
    _assert(checks, "entry_manager_syncs_cap_from_s1_budget",
            budget_rec["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and abs(budget_cap.deployed_usd - 12.0) < 1e-9
            and "selftest_budget_sync" in budget_mgr._attempted_one_shot_ids)

    s1_entry_ledger = os.path.join(OUT_DIR, "selftest_s1_entry_ledger_absent.jsonl")
    if os.path.exists(s1_entry_ledger):
        os.remove(s1_entry_ledger)
    s1_entry_mgr = EntryManager(
        win_provider=StubWinRateProvider(),
        executor=S1ExecutionAdapter({
            "kill_file": "/no/such/kill",
            "arm_state_file": "/no/such/arm.json",
            "ledger_path": s1_entry_ledger,
            "estimate_fill": False,
        }),
        cap_guard=CapGuard(25.0, 1.0),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        edge_buffer=0.05, fee_bps=0.0,
        candidate_fn=lambda: [dict(_base_intent)],
    )
    s1_entry_rec = s1_entry_mgr.tick(effective_mode="dry_run")[0]
    s1_entry_plan = (s1_entry_rec.get("execution") or {}).get("intent", {}).get("s1", {})
    _assert(checks, "agent_intent_private_signal_routes_to_s1_dry_plan",
            s1_entry_rec["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and (s1_entry_rec.get("execution") or {}).get("accepted") is True
            and (s1_entry_rec.get("execution") or {}).get("executed") is False
            and s1_entry_plan.get("mode") == "DRY_RUN_PLAN"
            and s1_entry_plan.get("would_place") is True)
    _assert(checks, "s1_entry_dry_plan_no_ledger_write", not os.path.exists(s1_entry_ledger))

    leak_ledger = os.path.join(OUT_DIR, "selftest_s1_leak_guard_ledger.jsonl")
    leak_arm = os.path.join(OUT_DIR, "selftest_s1_leak_guard_arm.json")
    leak_alert = os.path.join(OUT_DIR, "selftest_s1_leak_guard_alerts.jsonl")
    for path in (leak_ledger, leak_arm, leak_alert, tmp_global_halt):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    write_json(leak_arm, {
        "schema_version": pmx.ARM_STATE_SCHEMA_VERSION,
        "armed": True,
        "mode": "full",
        "live_ack": pmx.LIVE_ACK_PHRASE,
        "written_by": pmx.ARM_WRITER,
        "budget_epoch": "selftest-epoch",
        "budget_epoch_started_at": "2026-06-19T00:00:00Z",
    })
    saved_load_secrets = pmx.load_polymarket_secret_refs
    saved_build_client = pmx.build_secure_client
    saved_leak_guard = pmx.assert_no_secret_leak
    try:
        pmx.load_polymarket_secret_refs = lambda _secret_dir: {"private_key": "SELFTESTSECRET12345"}
        pmx.build_secure_client = lambda _secrets, *, secret_dir=pmx.DEFAULT_SECRET_DIR, side=None: pmx._FakeClient(wallet_type="DEPOSIT_WALLET")
        pmx.assert_no_secret_leak = lambda _record, _secrets: ["private_key"]
        leak_res = S1ExecutionAdapter(
            {
                "kill_file": "/no/such/kill",
                "arm_state_file": leak_arm,
                "ledger_path": leak_ledger,
                "estimate_fill": False,
            },
            alerter=Alerter(enabled=True, alert_file=leak_alert),
        ).execute(dummy_intent, effective_mode="live")
    finally:
        pmx.load_polymarket_secret_refs = saved_load_secrets
        pmx.build_secure_client = saved_build_client
        pmx.assert_no_secret_leak = saved_leak_guard
    leak_s1 = leak_res.intent.get("s1") if isinstance(leak_res.intent, dict) else {}
    leak_ledger_text = open(leak_ledger, encoding="utf-8").read() if os.path.exists(leak_ledger) else ""
    _assert(checks, "leak_guard_live_order_ledgered_before_halt",
            pmx.LIVE_PENDING_MODE in leak_ledger_text and pmx.LIVE_EXECUTED_MODE in leak_ledger_text)
    _assert(checks, "leak_guard_live_order_not_reported_failed",
            leak_res.accepted is True and leak_res.executed is True and leak_res.simulated is False)
    _assert(checks, "leak_guard_triggers_halt_and_alert",
            os.path.exists(tmp_global_halt)
            and os.path.exists(leak_alert)
            and "s1_secret_leak_guard_tripped" in open(leak_alert, encoding="utf-8").read()
            and ((leak_s1.get("secret_leak_guard") or {}).get("halt_engaged") is True))
    for path in (leak_ledger, leak_arm, leak_alert, tmp_global_halt):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    replay_queue = os.path.join(OUT_DIR, "selftest_agent_intents_replay.jsonl")
    replay_consumed = _agent_intent_consumed_path(replay_queue)
    for path in (replay_queue, replay_consumed):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    replay_key = "selftest_persistent_consumed_key"
    replay_row = dict(
        _base_intent,
        schema_version="polymarket-agent-intent-v0.1",
        created_at=iso_now(),
        expires_at="2999-01-01T00:00:00Z",
        source="marketflow_chat",
        idempotency_key=replay_key,
    )
    append_jsonl(replay_queue, replay_row)
    replay_loader = make_entry_candidate_fn(
        {
            "agent_intent_queue_files": [replay_queue],
            "agent_intent_queue_tail": 25,
            "enable_agent_intent_entries": True,
        },
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
    )
    replay_mgr = EntryManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        cap_guard=CapGuard(25.0, 1.0),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        edge_buffer=0.05, fee_bps=0.0,
        candidate_fn=replay_loader,
    )
    replay_first = replay_mgr.tick(effective_mode="dry_run")
    replay_loader_after_restart = make_entry_candidate_fn(
        {
            "agent_intent_queue_files": [replay_queue],
            "agent_intent_queue_tail": 25,
            "enable_agent_intent_entries": True,
        },
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
    )
    replay_mgr_after_restart = EntryManager(
        win_provider=StubWinRateProvider(),
        executor=DryRunStubExecutionAdapter(),
        cap_guard=CapGuard(25.0, 1.0),
        kill_switch=KillSwitch(kill_file="/no/such/kill"),
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        edge_buffer=0.05, fee_bps=0.0,
        candidate_fn=replay_loader_after_restart,
    )
    replay_second = replay_mgr_after_restart.tick(effective_mode="dry_run")
    _assert(checks, "agent_intent_consumed_key_persisted",
            len(replay_first) == 1
            and replay_first[0].get("decision") == "AGENT_INTENT_ENTRY_SIGNAL"
            and os.path.exists(replay_consumed)
            and replay_key in open(replay_consumed, encoding="utf-8").read())
    _assert(checks, "agent_intent_consumed_key_not_replayed_after_restart", replay_second == [])
    for path in (replay_queue, replay_consumed):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    compiler_queue = os.path.join(OUT_DIR, "selftest_compiler_intents.jsonl")
    compiler_consumed = _agent_intent_consumed_path(compiler_queue)
    for path in (compiler_queue, compiler_consumed):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    compiler_buy_key = "selftest_compiler_buy"
    chat_buy_key = "selftest_chat_buy"
    append_jsonl(compiler_queue, dict(
        _base_intent,
        schema_version="polymarket-agent-intent-v0.1",
        created_at=iso_now(),
        expires_at="2999-01-01T00:00:00Z",
        source="edge_compiler",
        idempotency_key=compiler_buy_key,
        compiler_version="selftest",
        feature_hash="sha256:selftestcompiler",
    ))
    append_jsonl(compiler_queue, dict(
        _base_intent,
        schema_version="polymarket-agent-intent-v0.1",
        created_at=iso_now(),
        expires_at="2999-01-01T00:00:00Z",
        source="marketflow_chat",
        idempotency_key=chat_buy_key,
    ))
    compiler_loader = make_entry_candidate_fn(
        {
            "compiler_intent_queue_files": [compiler_queue],
            "compiler_intent_queue_tail": 25,
            "enable_compiler_intents": True,
            "agent_intent_queue_files": [compiler_queue],
            "agent_intent_queue_tail": 25,
            "enable_agent_intent_entries": True,
        },
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
    )
    compiler_loaded = compiler_loader() if compiler_loader else []
    _assert(checks, "compiler_queue_precedes_chat_queue_without_duplicate",
            len(compiler_loaded) == 2
            and compiler_loaded[0].get("source") == "edge_compiler"
            and compiler_loaded[0].get("idempotency_key") == compiler_buy_key
            and compiler_loaded[1].get("source") == "marketflow_chat"
            and compiler_loaded[1].get("idempotency_key") == chat_buy_key)

    compiler_sell_key = "selftest_compiler_sell"
    append_jsonl(compiler_queue, {
        "schema_version": "polymarket-agent-intent-v0.1",
        "created_at": iso_now(),
        "expires_at": "2999-01-01T00:00:00Z",
        "source": "edge_compiler",
        "action": "SELL",
        "market_id": "m",
        "market_slug": "s",
        "token_id": "tok",
        "side": "YES",
        "shares": 1.0,
        "held_shares": 1.0,
        "min_price": 0.40,
        "allow_live_exit": True,
        "idempotency_key": compiler_sell_key,
    })
    compiler_sell_loader = make_entry_candidate_fn(
        {
            "compiler_intent_queue_files": [compiler_queue],
            "compiler_intent_queue_tail": 25,
            "enable_compiler_intents": True,
            "agent_intent_queue_files": [compiler_queue],
            "agent_intent_queue_tail": 25,
            "enable_agent_intent_entries": False,
        },
        alerter=Alerter(enabled=False, alert_file="/tmp/none"),
    )
    compiler_sell_loaded = compiler_sell_loader() if compiler_sell_loader else []
    sell_rows = [r for r in compiler_sell_loaded if r.get("idempotency_key") == compiler_sell_key]
    _assert(checks, "compiler_sell_not_blocked_by_agent_entry_toggle",
            len(sell_rows) == 1
            and sell_rows[0].get("candidate_type") == "agent_exit_intent"
            and sell_rows[0].get("allow_live_exit") is True)
    for path in (compiler_queue, compiler_consumed):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    auto_exp_ledger = os.path.join(OUT_DIR, "selftest_auto_experience_ledger.jsonl")
    auto_s1_ledger = os.path.join(OUT_DIR, "selftest_auto_s1_ledger_absent.jsonl")
    auto_config_path = os.path.join(OUT_DIR, "selftest_auto_config.json")
    for path in (auto_exp_ledger, auto_s1_ledger, auto_config_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    auto_config = default_config()
    auto_config.update({
        "position_source": "config",
        "positions": [],
        "win_rate_provider": "stub",
        "executor": "s1",
        "alert": {"enabled": True},
        "s1": {
            "kill_file": "/no/such/kill",
            "arm_state_file": "/no/such/arm.json",
            "ledger_path": auto_s1_ledger,
            "estimate_fill": False,
        },
        "autonomous_turn": {
            "enabled": True,
            "trigger_on_tick": True,
            "ledger_path": auto_exp_ledger,
            "mock_decision": {
                "turn_id": "selftest-auto-turn",
                "source": "daemon_selftest_mock",
                "market": {
                    "market_id": "0xauto",
                    "market_slug": "selftest-auto-market",
                    "token_id": "auto-token",
                    "side": "YES",
                    "order_min_size": 1.5,
                    "tick_size": 0.01,
                },
                "decision": "ENTRY_SIGNAL",
                "reason": "selftest autonomous structured entry",
                "research_summary": "selftest mock research",
                "info_sources_used": ["selftest"],
                "entry_price": 0.50,
                "size": 2.0,
                "predicted_prob": 0.62,
                "market_prob": 0.50,
                "intent": {
                    "market_order_type": "FOK",
                    "max_spend_usd": 1.0,
                    "max_price": 0.55,
                    "max_loss_usd": 1.0,
                    "resolution_confirmed_clean": True,
                    "resolution_source": "selftest",
                },
            },
        },
    })
    write_json(auto_config_path, auto_config)
    auto_args = argparse.Namespace(
        config=auto_config_path,
        lock_file=os.path.join(OUT_DIR, "selftest_auto.pidlock"),
        interval=None,
        risk_buffer=None,
        live=False,
        arm_state_file="/no/such/arm.json",
        kill_file="/no/such/kill",
        alert_file=os.path.join(OUT_DIR, "selftest_auto_alerts.jsonl"),
        secret_dir=DEFAULT_SECRET_DIR,
        executor=None,
        win_provider=None,
        ledger=os.path.join(OUT_DIR, "selftest_auto_daemon_ledger.jsonl"),
        latest_json=os.path.join(OUT_DIR, "selftest_auto_latest.json"),
        latest_summary=os.path.join(OUT_DIR, "selftest_auto_latest.md"),
        print_json=False,
        once=True,
        ticks=1,
        selftest=False,
    )
    auto_tick = AutotradeDaemon(auto_args).tick()
    auto_turn = auto_tick.get("autonomous_turn") or {}
    auto_exec = auto_turn.get("execution") or {}
    auto_plan = ((auto_exec.get("execution") or {}).get("intent") or {}).get("s1", {})
    auto_decision_row = trade_experience.latest_for_turn(auto_turn.get("turn_id"), ledger_path=auto_exp_ledger) or {}
    _assert(checks, "autonomous_mock_turn_records_structured_decision",
            (auto_turn.get("internal change log") or {}).get("decision") == "ENTRY_SIGNAL"
            and auto_decision_row)
    _assert(checks, "autonomous_turn_routes_to_s1_dry_plan",
            auto_exec.get("decision") == "AGENT_INTENT_ENTRY_SIGNAL"
            and ((auto_exec.get("execution") or {}).get("executed") is False)
            and auto_plan.get("mode") == "DRY_RUN_PLAN"
            and auto_plan.get("would_place") is True)
    _assert(checks, "autonomous_turn_no_s1_ledger_write_in_dry_run", not os.path.exists(auto_s1_ledger))
    _assert(checks, "autonomous_metrics_read_temp_ledger",
            (auto_tick.get("experience_metrics") or {}).get("principal_usd") == trade_experience.PRINCIPAL_USD)

    auto_sell_daemon = AutotradeDaemon(auto_args)
    sell_decision = {
        "experience_id": "selftest_auto_sell_over",
        "market": {
            "market_id": "0xsell",
            "market_slug": "selftest-sell-market",
            "token_id": "sell-token",
            "side": "YES",
        },
        "decision": "SELL_SIGNAL",
        "reason": "selftest autonomous sell",
        "entry_price": 0.40,
        "size": 2.0,
        "intent": {"min_price": 0.40, "market_order_type": "FAK"},
    }
    auto_sell_over = auto_sell_daemon._execute_autonomous_sell(
        sell_decision,
        effective_mode="dry_run",
        positions=[{"market_slug": "selftest-sell-market", "token_id": "sell-token", "side": "YES", "shares": 1.0, "entry_cost": 0.50}],
    )
    auto_sell_refusals = (((auto_sell_over.get("intent") or {}).get("s1") or {}).get("fuses") or {}).get("refusals", [])
    _assert(checks, "autonomous_sell_real_held_triggers_s1_naked_short_fuse",
            auto_sell_over.get("accepted") is False
            and any("naked short" in str(item).lower() for item in auto_sell_refusals)
            and ((auto_sell_over.get("intent") or {}).get("daemon") or {}).get("held_shares") == 1.0)
    auto_sell_alert = auto_args.alert_file
    try:
        os.remove(auto_sell_alert)
    except FileNotFoundError:
        pass
    auto_sell_missing = auto_sell_daemon._execute_autonomous_sell(
        sell_decision,
        effective_mode="dry_run",
        positions=[],
    )
    _assert(checks, "autonomous_sell_missing_position_alerts_and_refuses",
            auto_sell_missing.get("accepted") is False
            and "held_shares unavailable" in str(auto_sell_missing.get("reason"))
            and os.path.exists(auto_sell_alert)
            and "autonomous_sell_position_unavailable" in open(auto_sell_alert, encoding="utf-8").read())

    pos_fail_alert_file = os.path.join(OUT_DIR, "selftest_position_discovery_alerts.jsonl")
    pos_fail_config_path = os.path.join(OUT_DIR, "selftest_position_discovery_fail_config.json")
    for path in (pos_fail_alert_file, pos_fail_config_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _write_arm(mode="full")
    pos_fail_config = default_config()
    pos_fail_config.update({
        "position_source": "live_account",
        "positions": [],
        "win_rate_provider": "stub",
        "executor": "stub",
        "entry_candidates": [dict(_base_intent, candidate_type="ordinary", probability_override=0.70)],
        "autonomous_turn": {"enabled": True, "trigger_on_tick": True, "min_interval_sec": 0, "mock_decision": {"decision": "OBSERVE_ONLY"}},
        "alert": {"enabled": True},
    })
    write_json(pos_fail_config_path, pos_fail_config)
    saved_discover = globals()["discover_positions_with_meta"]

    def _raise_position_discovery(*_args, **_kwargs):
        raise DaemonError("synthetic position discovery failure")

    try:
        globals()["discover_positions_with_meta"] = _raise_position_discovery
        pos_fail_args = argparse.Namespace(
            config=pos_fail_config_path,
            lock_file=os.path.join(OUT_DIR, "selftest_position_discovery_fail.pidlock"),
            interval=None,
            risk_buffer=None,
            live=True,
            arm_state_file=arm_path,
            kill_file="/no/such/kill",
            alert_file=pos_fail_alert_file,
            secret_dir=DEFAULT_SECRET_DIR,
            executor=None,
            win_provider=None,
            ledger=os.path.join(OUT_DIR, "selftest_position_discovery_fail_ledger.jsonl"),
            latest_json=os.path.join(OUT_DIR, "selftest_position_discovery_fail_latest.json"),
            latest_summary=os.path.join(OUT_DIR, "selftest_position_discovery_fail_latest.md"),
            print_json=False,
            once=True,
            ticks=1,
            selftest=False,
        )
        pos_fail_tick = AutotradeDaemon(pos_fail_args).tick()
    finally:
        globals()["discover_positions_with_meta"] = saved_discover
        try:
            os.remove(arm_path)
        except FileNotFoundError:
            pass
    _assert(checks, "position_discovery_failure_alerts_and_skips_entry",
            (pos_fail_tick.get("position_discovery") or {}).get("status") == "failed"
            and pos_fail_tick.get("entries") == []
            and (pos_fail_tick.get("autonomous_turn") or {}).get("triggered") is False
            and os.path.exists(pos_fail_alert_file)
            and "position_discovery_failed" in open(pos_fail_alert_file, encoding="utf-8").read())

    saved_fetch_positions = monitor.fetch_private_open_position_configs
    captured_fetch: dict[str, Any] = {}

    def _fake_fetch_positions(**kwargs):
        captured_fetch.update(kwargs)
        configs = [
            {"market_slug": "p1", "token_id": "t1", "side": "YES", "shares": 1.0},
            {"market_slug": "p2", "token_id": "t2", "side": "YES", "shares": 1.0},
            {"market_slug": "p3", "token_id": "t3", "side": "YES", "shares": 1.0},
        ]
        return configs, {
            "truncated_by_max_positions": False,
            "total_count": 3,
            "pages_seen": 2,
            "has_more": False,
        }

    try:
        monitor.fetch_private_open_position_configs = _fake_fetch_positions
        all_positions, all_meta = discover_positions_with_meta(
            {"position_source": "live_account", "max_positions": 1, "page_size": 2},
            secret_dir=DEFAULT_SECRET_DIR,
            alerter=Alerter(enabled=False, alert_file="/tmp/none"),
        )
    finally:
        monitor.fetch_private_open_position_configs = saved_fetch_positions
    _assert(checks, "exit_position_discovery_ignores_config_max_positions",
            len(all_positions) == 3
            and captured_fetch.get("max_positions") == sys.maxsize
            and all_meta.get("configured_max_positions_ignored_for_exit") is True)

    run_alert_file = os.path.join(OUT_DIR, "selftest_run_loop_alerts.jsonl")
    run_config_path = os.path.join(OUT_DIR, "selftest_run_loop_config.json")
    for path in (run_alert_file, run_config_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    run_config = default_config()
    run_config.update({"interval_seconds": 0.001, "alert": {"enabled": True}})
    write_json(run_config_path, run_config)
    run_args = argparse.Namespace(
        config=run_config_path,
        lock_file=os.path.join(OUT_DIR, "selftest_run_loop.pidlock"),
        interval=None,
        risk_buffer=None,
        live=False,
        arm_state_file="/no/such/arm.json",
        kill_file="/no/such/kill",
        alert_file=run_alert_file,
        secret_dir=DEFAULT_SECRET_DIR,
        executor=None,
        win_provider=None,
        ledger=os.path.join(OUT_DIR, "selftest_run_loop_ledger.jsonl"),
        latest_json=os.path.join(OUT_DIR, "selftest_run_loop_latest.json"),
        latest_summary=os.path.join(OUT_DIR, "selftest_run_loop_latest.md"),
        print_json=False,
        once=False,
        ticks=2,
        selftest=False,
    )
    run_probe = AutotradeDaemon(run_args)
    run_calls: list[str] = []
    run_emitted: list[dict] = []

    def _tick_fail_then_ok():
        run_calls.append("tick")
        if len(run_calls) == 1:
            raise RuntimeError("synthetic tick file failure")
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": iso_now(),
            "tick_id": "selftest-run-ok",
            "daemon_mode": "off",
            "boundaries": BOUNDARIES,
            "live_gate": {"effective_mode": "dry_run"},
            "kill_switch": {"active": False},
            "caps": run_probe.cap_guard.status(),
            "executor": run_probe.executor.name,
            "win_rate_provider": run_probe.win_provider.name,
            "positions": [],
            "entries": [],
            "position_discovery": {"status": "ok", "positions_state": "confirmed_empty"},
        }

    run_probe.tick = _tick_fail_then_ok
    run_probe._emit = lambda record: run_emitted.append(record)
    run_rc = run_probe.run()
    _assert(checks, "run_loop_survives_single_tick_exception",
            run_rc == 0
            and len(run_calls) == 2
            and any((r.get("daemon_loop_error") or {}).get("error") == "synthetic tick file failure" for r in run_emitted)
            and any(r.get("tick_id") == "selftest-run-ok" for r in run_emitted)
            and os.path.exists(run_alert_file)
            and "daemon_tick_failed" in open(run_alert_file, encoding="utf-8").read())

    missing_resolution = dict(_base_intent)
    missing_resolution.pop("resolution_confirmed_clean")
    missing_rec = _entry_mgr([missing_resolution]).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_missing_resolution_rejected",
            missing_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in missing_rec.get("reject_reason_codes", []))
    dirty_rec = _entry_mgr([dict(_base_intent, resolution_confirmed_clean=False)]).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_dirty_resolution_rejected",
            dirty_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in dirty_rec.get("reject_reason_codes", []))
    dirty_strict_rec = _entry_mgr(
        [dict(_base_intent, resolution_confirmed_clean=False)],
        market_quality={"require_resolution": False},
    ).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_dirty_resolution_config_false_still_rejected",
            dirty_strict_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in dirty_strict_rec.get("reject_reason_codes", []))
    self_attested_alert = os.path.join(OUT_DIR, "selftest_self_attested_alerts.jsonl")
    try:
        os.remove(self_attested_alert)
    except FileNotFoundError:
        pass
    self_attested_rec = _entry_mgr(
        [dict(_base_intent, idempotency_key="selftest_self_attested_resolution")],
        alerter=Alerter(enabled=True, alert_file=self_attested_alert),
    ).tick(effective_mode="dry_run")[0]
    _assert(checks, "agent_intent_self_attested_resolution_passes_with_alert",
            self_attested_rec["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and (self_attested_rec.get("resolution_attestation") or {}).get("self_attested") is True
            and os.path.exists(self_attested_alert)
            and "entry_resolution_self_attested" in open(self_attested_alert, encoding="utf-8").read())

    # --- independent resolution verifier integration (belief-organ path) -------
    # A belief-organ-style intent ships resolution_confirmed_clean=False (never
    # self-attests). With the verifier supplying an INDEPENDENT clean attestation,
    # the SAME fuse now passes; the attestation is independent (not self-attested);
    # a not-clean verdict still fails closed; verifier errors fail closed.
    belief_intent = dict(
        _base_intent, source="edge_compiler", resolution_confirmed_clean=False,
        idempotency_key="selftest_verifier_clean",
    )

    def _clean_verifier(_candidate):
        return {"clean": True, "attestation_source": rverify.ATTESTATION_SOURCE, "reasons": []}

    def _dirty_verifier(_candidate):
        return {"clean": False, "attestation_source": None, "reasons": ["non_objective_resolution_domain"]}

    def _boom_verifier(_candidate):
        raise RuntimeError("verifier exploded")

    verifier_clean_rec = _entry_mgr([belief_intent], resolution_verifier=_clean_verifier).tick(effective_mode="dry_run")[0]
    _assert(checks, "verifier_independent_clean_passes_gate",
            verifier_clean_rec["decision"] == "AGENT_INTENT_ENTRY_SIGNAL"
            and (verifier_clean_rec.get("resolution_attestation") or {}).get("confirmed_clean_for_gate") is True
            and (verifier_clean_rec.get("resolution_attestation") or {}).get("self_attested") is False
            and (verifier_clean_rec.get("resolution_verifier") or {}).get("clean") is True)
    verifier_dirty_rec = _entry_mgr(
        [dict(belief_intent, idempotency_key="selftest_verifier_dirty")],
        resolution_verifier=_dirty_verifier,
    ).tick(effective_mode="dry_run")[0]
    _assert(checks, "verifier_not_clean_still_rejected",
            verifier_dirty_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in verifier_dirty_rec.get("reject_reason_codes", []))
    verifier_err_rec = _entry_mgr(
        [dict(belief_intent, idempotency_key="selftest_verifier_err")],
        resolution_verifier=_boom_verifier,
    ).tick(effective_mode="dry_run")[0]
    _assert(checks, "verifier_error_fails_closed",
            verifier_err_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in verifier_err_rec.get("reject_reason_codes", []))

    # Verifier never overrides an already-disabled (None) verifier: legacy path.
    verifier_off_rec = _entry_mgr([dict(belief_intent, idempotency_key="selftest_verifier_off")]).tick(effective_mode="dry_run")[0]
    _assert(checks, "verifier_disabled_keeps_failclosed",
            verifier_off_rec["decision"] == "NO_ENTRY_QUALITY_GATE_REJECT"
            and "RESOLUTION_NOT_CONFIRMED_CLEAN" in verifier_off_rec.get("reject_reason_codes", []))

    cap_report = cap_consistency_report(
        runtime_path("execution",
            "canary_readiness",
            "marketflow_live_account_exit_first_daemon.json",)
    )
    _assert(checks, "cap_sources_consistent", cap_report["ok"])

    # Every fee-rate read went through the injected stub, so this self-test opened no
    # socket. The read is fail-soft, which is exactly why it needs asserting: a real
    # network call would silently succeed here and silently make the result depend on
    # the venue being up.
    _assert(checks, "selftest_opened_no_network_connection",
            all(u.startswith(GAMMA_MARKETS_URL) for u in fee_fetch_calls)
            and all(isinstance(t, str) for t in book_fetch_calls))

    ok = all(checks.values())
    report = {
        "schema_version": "polymarket-autotrade-daemon-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": ok,
        "checks": checks,
        "cap_consistency": cap_report,
    }
    write_json(DEFAULT_SELFTEST, report)
    write_json(DEFAULT_EXAMPLE_CONFIG, _example_config())
    pmx.DEFAULT_GLOBAL_HALT_FILE = saved_global_halt
    return report


def load_daemon_config_from_obj(obj: dict) -> dict:
    monitor.validate_no_sensitive_config(obj)
    merged = default_config()
    merged.update(obj)
    return merged


def _fake_tick(*, decision: str, break_even: float, win_p: float, decision_extra: dict | None = None) -> dict:
    dec = {"decision": decision, "reason": "selftest synthetic"}
    if decision_extra:
        dec.update(decision_extra)
    return {
        "decision": dec,
        "valuation": {
            "break_even_probability": break_even,
            "immediate_exit_value": round(break_even * 10.0, 8),
            "sweep": {"terminal_price": break_even, "full_fill": True},
        },
        "market": {"market_id": "0xtest", "slug": "selftest-market"},
        "token": {"token_id": "123"},
        "position": {"side": "YES", "shares": 10.0, "entry_cost": 5.0},
    }


def _example_config() -> dict:
    return {
        "interval_seconds": 5,
        "risk_buffer": 0.03,
        "capital": {"total_cap_usd": DEFAULT_TOTAL_CAP_USD, "per_trade_cap_usd": DEFAULT_PER_TRADE_CAP_USD},
        "position_source": "config",
        "positions": [
            {
                "market_slug": "example-market-slug",
                "token_id": "OPTIONAL_EXPLICIT_TOKEN_ID",
                "side": "YES",
                "shares": 10.0,
                "entry_cost": 5.0,
                "probability_override": 0.55,
            }
        ],
        "win_rate_provider": "stub",
        "executor": "stub",
        "s1": {
            "secret_dir": os.path.join(os.path.expanduser("~"), ".marketflow", "secrets"),
            "estimate_fill": False,
            "exit_order_kind": "limit",
            "max_total_deploy_usd": pmx.DEFAULT_MAX_TOTAL_DEPLOY_USD,
            "max_per_trade_usd": pmx.DEFAULT_MAX_PER_TRADE_USD
        },
        "s2": {
            "secret_dir": os.path.join(os.path.expanduser("~"), ".marketflow", "secrets"),
            "mu_total": 2.6
        },
        "market_quality": dict(DEFAULT_MARKET_QUALITY),
        # MarketFlow chat -> autotrade channel: when enabled, the daemon consumes the
        # BUY intents MarketFlow queues via the polymarket_intent_enqueue tool. Default
        # OFF (safe): autonomous entry stays a deliberate opt-in. Every queued
        # intent still passes objective market filters (longshot/band), private
        # edge policy, caps, and arm-state before any sign/POST.
        "enable_agent_intent_entries": False,
        "agent_intent_queue_files": [DEFAULT_AGENT_INTENT_QUEUE],
        "agent_intent_queue_tail": 25,
        # Deterministic edge compiler -> same durable daemon/S1 queue. Default ON
        # because only source=edge_compiler rows are read here; no compiler row
        # means no behavior change. S1 fuses and arm-state remain final.
        "enable_compiler_intents": True,
        "compiler_intent_queue_files": [DEFAULT_AGENT_INTENT_QUEUE],
        "compiler_intent_queue_tail": 25,
        # Autonomous turn path: daemon posts a dry-run prompt to bridge, bridge
        # queues MarketFlowChatStart(window=auto), and the engine-native
        # polymarket_internal change log tool writes the structured decision. Default
        # OFF; no bridge/daemon LLM fallback.
        "autonomous_turn": {
            "enabled": False,
            "bridge_url": DEFAULT_BRIDGE_AUTONOMOUS_URL,
            "timeout_sec": 180,
            "poll_sec": 2,
            "trigger_on_tick": False,
            "ledger_path": trade_experience.DEFAULT_LEDGER,
        },
        "enable_supervised_canary_entries": False,
        # Independent clean-resolution verifier: read-only Gamma/UMA metadata pull
        # that supplies a non-self-attested clean attestation for objective
        # (sports/esports) + standard-UMA + undisputed markets, so belief-organ /
        # compiler intents that never self-attest can still clear the resolution
        # fuse. Fail-closed; never arms or weakens caps/kill/arm-state. Set
        # enabled=false to revert to attestation-only (legacy) behaviour.
        "resolution_verifier": {"enabled": True, "fetch_timeout_sec": rverify.DEFAULT_FETCH_TIMEOUT},
        "alert": {"enabled": False},
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Polymarket autonomous loop daemon v0.1 (exit-first, dry_run default).")
    p.add_argument("--config", help="Daemon config JSON (defaults to a safe dry_run config).")
    p.add_argument("--live", action="store_true", help="Request live execution (still needs an armed unified arm-state).")
    p.add_argument("--arm-state-file", default=DEFAULT_ARM_STATE_FILE, help="Unified arm-state JSON (shared with S1; bridge-written).")
    p.add_argument("--kill-file", default=DEFAULT_KILL_FILE, help="Presence of this file halts all execution.")
    p.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="Single-instance daemon pid lock.")
    p.add_argument("--secret-dir", default=DEFAULT_SECRET_DIR, help="Secret refs dir for live_account position discovery.")
    p.add_argument("--executor", choices=["stub", "s1"], help="Execution adapter; overrides config.executor (default stub).")
    p.add_argument("--win-provider", choices=["stub"],
                   help="Win-rate provider; overrides config.win_rate_provider "
                        "(default stub).")
    p.add_argument("--risk-buffer", type=float, help="Probability margin around the break-even exit threshold.")
    p.add_argument("--interval", type=float, help="Seconds between ticks.")
    p.add_argument("--ticks", type=int, default=1, help="Number of ticks; 0 means run until interrupted.")
    p.add_argument("--once", action="store_true", help="Run exactly one tick.")
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--latest-json", default=DEFAULT_LATEST_JSON)
    p.add_argument("--latest-summary", default=DEFAULT_LATEST_SUMMARY)
    p.add_argument("--alert-file", default=DEFAULT_ALERT_FILE)
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["PASS"] else 1
    if args.interval is not None and args.interval <= 0:
        print("error: --interval must be positive", file=sys.stderr)
        return 2
    if args.ticks < 0:
        print("error: --ticks must be >= 0", file=sys.stderr)
        return 2
    try:
        daemon = AutotradeDaemon(args)
        return daemon.run()
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
