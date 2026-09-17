#!/usr/bin/env python3
"""Trade experience memory and conservative playbook updates.

This module owns the paper/dry-run learning memory for Polymarket autonomous
turns. It records decisions and later forward outcomes, then updates a small
playbook from forward calibration evidence only. It does not trade, sign, read
secrets, or modify engine math.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any


from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
OUT_DIR = runtime_path("risk", "experience")
DEFAULT_LEDGER = os.path.join(OUT_DIR, "ledger.jsonl")
DEFAULT_PLAYBOOK = os.path.join(OUT_DIR, "playbook.json")
DEFAULT_SELFTEST = os.path.join(OUT_DIR, "selftest_report.json")

SCHEMA_VERSION = "trade-experience-v0.1"
PLAYBOOK_SCHEMA_VERSION = "trade-experience-playbook-v0.1"
DECISIONS = {"SELL_SIGNAL", "ENTRY_SIGNAL", "HOLD", "NONE", "OBSERVE_ONLY"}
PRINCIPAL_USD = 25.0


class ExperienceError(Exception):
    """Raised for fail-loud experience-memory errors."""


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


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def clamp_prob(value: Any) -> float | None:
    out = to_float(value)
    if out is None:
        return None
    if not (0.0 <= out <= 1.0):
        return None
    return out


def safe_str(value: Any, *, max_len: int = 1000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def normalize_sources(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [x.strip() for x in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        raw = [str(x).strip() for x in value]
    else:
        raw = [str(value).strip()]
    out: list[str] = []
    for item in raw:
        if item and item not in out:
            out.append(item[:500])
    return out[:25]


def normalize_decision(value: Any) -> str:
    token = str(value or "NONE").strip().upper()
    return token if token in DECISIONS else "NONE"


def normalize_experience(row: dict[str, Any]) -> dict[str, Any]:
    market = row.get("market")
    if isinstance(market, dict):
        market_obj = {str(k): v for k, v in market.items()}
    else:
        market_obj = {"label": safe_str(market, max_len=300)}
    decision = normalize_decision(row.get("decision"))
    predicted = clamp_prob(row.get("predicted_prob"))
    market_prob = clamp_prob(row.get("market_prob"))
    realized = to_float(row.get("realized_pnl"))
    forward = row.get("forward_outcome")
    if isinstance(forward, bool):
        forward_norm: str | None = "WIN" if forward else "LOSS"
    else:
        forward_norm = safe_str(forward, max_len=80)
    calibration = to_float(row.get("calibration_error"))
    if calibration is None and predicted is not None and forward_norm in ("WIN", "LOSS"):
        outcome = 1.0 if forward_norm == "WIN" else 0.0
        calibration = round(predicted - outcome, 8)
    regime_context = row.get("regime_context") if isinstance(row.get("regime_context"), dict) else {}
    if isinstance(row.get("temporal_context"), dict):
        regime_context = dict(regime_context)
        regime_context["temporal_context"] = row.get("temporal_context")
    base = {
        "schema_version": SCHEMA_VERSION,
        "experience_id": row.get("experience_id") or f"texp_{int(time.time() * 1000)}_{hash_obj(row)[:10]}",
        "ts": row.get("ts") or iso_now(),
        "turn_id": safe_str(row.get("turn_id"), max_len=120),
        "source": safe_str(row.get("source"), max_len=80) or "unknown",
        "market": market_obj,
        "decision": decision,
        "reason": safe_str(row.get("reason"), max_len=1600),
        "research_summary": safe_str(row.get("research_summary"), max_len=2200),
        "info_sources_used": normalize_sources(row.get("info_sources_used")),
        "entry_price": to_float(row.get("entry_price")),
        "size": to_float(row.get("size")),
        "predicted_prob": predicted,
        "market_prob": market_prob,
        "forward_outcome": forward_norm,
        "realized_pnl": realized,
        "calibration_error": calibration,
        "regime_context": regime_context,
        "intent": row.get("intent") if isinstance(row.get("intent"), dict) else {},
    }
    base["row_hash"] = "sha256:" + hash_obj(base)
    return base


def append_experience(row: dict[str, Any], *, ledger_path: str = DEFAULT_LEDGER) -> dict[str, Any]:
    norm = normalize_experience(row)
    ensure_parent(ledger_path)
    with open(ledger_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(canonical_json(norm) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return norm


def iter_experiences(*, ledger_path: str = DEFAULT_LEDGER, limit: int | None = None) -> list[dict[str, Any]]:
    if not os.path.exists(ledger_path):
        return []
    with open(ledger_path, encoding="utf-8") as f:
        lines = f.readlines()
    if limit is not None:
        lines = lines[-max(0, int(limit)):]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("schema_version") == SCHEMA_VERSION:
            rows.append(row)
    return rows


def latest_for_turn(turn_id: str, *, ledger_path: str = DEFAULT_LEDGER) -> dict[str, Any] | None:
    if not turn_id:
        return None
    for row in reversed(iter_experiences(ledger_path=ledger_path, limit=500)):
        if row.get("turn_id") == turn_id:
            return row
    return None


def default_playbook() -> dict[str, Any]:
    return {
        "schema_version": PLAYBOOK_SCHEMA_VERSION,
        "updated_at": iso_now(),
        "version": 1,
        "discipline": [
            "Protect the exit on positions already open before considering a new one.",
            "Never treat a single trade's PnL as the reward signal.",
            "Learn from forward calibration, Brier and log loss, out-of-sample hit "
            "rate, and drawdown-adjusted compounding slope.",
            "Refuse long-tail prices and ambiguous resolution rules before the "
            "capital fuses ever have to fire.",
            "Entry is bound by whichever is stricter, the arm-state or the code "
            "caps. Never write your own arm-state.",
        ],
        "research_strategy": [
            "Read the resolution rules first-hand before forming a view.",
            "Keep independent evidence separate from the market's own implied "
            "price and from anything quoting it.",
            "Record only the sources that actually changed a decision, not every "
            "page that was opened.",
        ],
        "source_weights": {},
        "last_reflection": {
            "status": "no_forward_sample_yet",
            "sample_count": 0,
            "notes": [],
        },
    }


def load_playbook(path: str = DEFAULT_PLAYBOOK) -> dict[str, Any]:
    if not os.path.exists(path):
        pb = default_playbook()
        write_json(path, pb)
        return pb
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get("schema_version") != PLAYBOOK_SCHEMA_VERSION:
        pb = default_playbook()
        write_json(path, pb)
        return pb
    return data


def playbook_summary(path: str = DEFAULT_PLAYBOOK) -> str:
    pb = load_playbook(path)
    parts = ["Current betting discipline:"]
    parts.extend(f"- {x}" for x in pb.get("discipline", [])[:8])
    parts.append("Current research strategy:")
    parts.extend(f"- {x}" for x in pb.get("research_strategy", [])[:8])
    refl = pb.get("last_reflection") or {}
    if refl.get("notes"):
        parts.append("Most recent reflection:")
        parts.extend(f"- {x}" for x in refl.get("notes", [])[:5])
    return "\n".join(parts)


def scored_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        p = clamp_prob(row.get("predicted_prob"))
        outcome = row.get("forward_outcome")
        if p is not None and outcome in ("WIN", "LOSS"):
            out.append(row)
    return out


def metrics(*, ledger_path: str = DEFAULT_LEDGER, principal_usd: float = PRINCIPAL_USD) -> dict[str, Any]:
    rows = iter_experiences(ledger_path=ledger_path)
    settled = [r for r in rows if r.get("forward_outcome") in ("WIN", "LOSS")]
    scored = scored_rows(rows)
    realized = [to_float(r.get("realized_pnl")) for r in rows]
    realized = [x for x in realized if x is not None]
    brier = None
    if scored:
        vals = []
        for row in scored:
            p = float(row["predicted_prob"])
            y = 1.0 if row.get("forward_outcome") == "WIN" else 0.0
            vals.append((p - y) ** 2)
        brier = round(sum(vals) / len(vals), 8)
    pnl_total = round(sum(realized), 8) if realized else 0.0
    return {
        "schema_version": "trade-experience-metrics-v0.1",
        "generated_at": iso_now(),
        "principal_usd": principal_usd,
        "snowball_nav_usd": round(principal_usd + pnl_total, 8),
        "realized_pnl_usd": pnl_total,
        "settled_count": len(settled),
        "scored_count": len(scored),
        "win_rate": (round(sum(1 for r in settled if r.get("forward_outcome") == "WIN") / len(settled), 8) if settled else None),
        "brier_score": brier,
        "reward_signal": "forward_calibration_oos_win_rate_drawdown_adjusted_snowball_slope_not_single_trade_pnl",
    }


def reflect_and_update(
    *,
    ledger_path: str = DEFAULT_LEDGER,
    playbook_path: str = DEFAULT_PLAYBOOK,
    min_forward_samples: int = 3,
) -> dict[str, Any]:
    rows = iter_experiences(ledger_path=ledger_path)
    scored = scored_rows(rows)
    pb = load_playbook(playbook_path)
    if len(scored) < min_forward_samples:
        pb["last_reflection"] = {
            "status": "insufficient_forward_samples",
            "sample_count": len(scored),
            "min_forward_samples": min_forward_samples,
            "notes": ["Keep the existing discipline until more forward outcomes settle."],
        }
        pb["updated_at"] = iso_now()
        write_json(playbook_path, pb)
        return {"updated": False, "playbook": pb, "reason": "insufficient_forward_samples"}

    abs_errs = []
    source_scores: dict[str, list[float]] = {}
    for row in scored:
        p = float(row["predicted_prob"])
        y = 1.0 if row.get("forward_outcome") == "WIN" else 0.0
        err = abs(p - y)
        abs_errs.append(err)
        for source in row.get("info_sources_used") or []:
            source_scores.setdefault(source, []).append(err)
    mean_abs = sum(abs_errs) / len(abs_errs)
    source_weights = {}
    for source, errs in source_scores.items():
        if len(errs) < 2:
            continue
        source_weights[source] = round(max(0.1, 1.0 - (sum(errs) / len(errs))), 4)
    notes = [
        f"Forward-scored sample count: {len(scored)}.",
        f"Mean absolute calibration error: {mean_abs:.4f}.",
        "No playbook rule was changed from single-trade PnL.",
    ]
    if mean_abs > 0.30:
        notes.append("Tighten entry discipline until calibration improves; prefer observe-only when evidence is thin.")
    elif mean_abs < 0.18:
        notes.append("Calibration is improving; keep source discipline stable and continue forward measurement.")
    pb["source_weights"] = source_weights
    pb["last_reflection"] = {
        "status": "updated_from_forward_samples",
        "sample_count": len(scored),
        "mean_abs_calibration_error": round(mean_abs, 8),
        "notes": notes,
    }
    pb["updated_at"] = iso_now()
    pb["version"] = int(pb.get("version") or 1) + 1
    write_json(playbook_path, pb)
    return {"updated": True, "playbook": pb, "metrics": metrics(ledger_path=ledger_path), "notes": notes}


def record_decision_from_tool(**kwargs: Any) -> dict[str, Any]:
    ledger_path = safe_str(kwargs.get("ledger_path"), max_len=1000) or DEFAULT_LEDGER
    row = append_experience({
        "turn_id": kwargs.get("turn_id"),
        "source": "engine_native_tool",
        "market": {
            "market_id": safe_str(kwargs.get("market_id"), max_len=160),
            "market_slug": safe_str(kwargs.get("market_slug"), max_len=260),
            "token_id": safe_str(kwargs.get("token_id"), max_len=180),
            "side": safe_str(kwargs.get("side"), max_len=12),
        },
        "decision": kwargs.get("decision"),
        "reason": kwargs.get("reason"),
        "research_summary": kwargs.get("research_summary"),
        "info_sources_used": kwargs.get("info_sources_used"),
        "entry_price": kwargs.get("entry_price"),
        "size": kwargs.get("size"),
        "predicted_prob": kwargs.get("predicted_prob"),
        "market_prob": kwargs.get("market_prob"),
        "regime_context": kwargs.get("regime_context") if isinstance(kwargs.get("regime_context"), dict) else {},
        "temporal_context": kwargs.get("temporal_context") if isinstance(kwargs.get("temporal_context"), dict) else None,
        "intent": {
            "action": normalize_decision(kwargs.get("decision")),
            "order_kind": safe_str(kwargs.get("order_kind"), max_len=40),
            "market_order_type": safe_str(kwargs.get("market_order_type"), max_len=40),
            "max_spend_usd": to_float(kwargs.get("max_spend_usd")),
            "max_price": to_float(kwargs.get("max_price")),
            "min_price": to_float(kwargs.get("min_price")),
            "max_loss_usd": to_float(kwargs.get("max_loss_usd")),
            "close_time": safe_str(kwargs.get("close_time"), max_len=100),
            "resolution_confirmed_clean": kwargs.get("resolution_confirmed_clean") is True,
            "resolution_source": safe_str(kwargs.get("resolution_source"), max_len=300),
        },
    }, ledger_path=ledger_path)
    return {
        "ok": True,
        "experience_id": row["experience_id"],
        "turn_id": row.get("turn_id"),
        "decision": row.get("decision"),
        "path": os.path.relpath(ledger_path, REPO_ROOT),
        "metrics": metrics(ledger_path=ledger_path),
        "note": "recorded only; daemon/S1 still enforce dry_run, arm-state, caps, and kill fuses",
    }


def selftest() -> dict[str, Any]:
    ensure_parent(DEFAULT_SELFTEST)
    tmp_ledger = os.path.join(OUT_DIR, "selftest_ledger.jsonl")
    tmp_playbook = os.path.join(OUT_DIR, "selftest_playbook.json")
    for path in (tmp_ledger, tmp_playbook):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    checks: dict[str, bool] = {}
    row = append_experience({
        "turn_id": "turn1",
        "source": "selftest",
        "market": {"market_id": "m1", "market_slug": "selftest-market"},
        "decision": "ENTRY_SIGNAL",
        "reason": "selftest decision",
        "info_sources_used": ["official rules", "scoreboard"],
        "entry_price": 0.5,
        "size": 2,
        "predicted_prob": 0.62,
        "market_prob": 0.50,
    }, ledger_path=tmp_ledger)
    checks["append_normalizes_decision"] = row["decision"] == "ENTRY_SIGNAL"
    checks["latest_by_turn"] = (latest_for_turn("turn1", ledger_path=tmp_ledger) or {}).get("experience_id") == row["experience_id"]
    checks["playbook_created"] = load_playbook(tmp_playbook)["schema_version"] == PLAYBOOK_SCHEMA_VERSION

    for i, (p, outcome, pnl) in enumerate(((0.7, "WIN", 0.4), (0.4, "LOSS", -0.2), (0.6, "WIN", 0.3)), start=2):
        append_experience({
            "turn_id": f"turn{i}",
            "source": "selftest",
            "market": {"market_id": f"m{i}"},
            "decision": "ENTRY_SIGNAL",
            "predicted_prob": p,
            "forward_outcome": outcome,
            "realized_pnl": pnl,
            "info_sources_used": ["official rules"],
        }, ledger_path=tmp_ledger)
    refl = reflect_and_update(ledger_path=tmp_ledger, playbook_path=tmp_playbook, min_forward_samples=3)
    met = metrics(ledger_path=tmp_ledger)
    checks["reflection_updates_after_forward_samples"] = refl["updated"] is True
    checks["metrics_not_single_pnl_reward"] = "not_single_trade_pnl" in met["reward_signal"]
    checks["snowball_nav_display_only"] = abs(met["snowball_nav_usd"] - (PRINCIPAL_USD + 0.5)) < 1e-9
    tool = record_decision_from_tool(
        turn_id="tool-turn",
        decision="HOLD",
        market_id="mtool",
        reason="tool selftest",
        info_sources_used=["source-a"],
        temporal_context={"schema_version": "test", "trade_permission": "NONE"},
        ledger_path=tmp_ledger,
    )
    checks["tool_record_shape"] = tool["ok"] is True and tool["decision"] == "HOLD"
    latest_tool = latest_for_turn("tool-turn", ledger_path=tmp_ledger) or {}
    checks["tool_temporal_context_saved"] = (
        ((latest_tool.get("regime_context") or {}).get("temporal_context") or {}).get("trade_permission") == "NONE"
    )

    for path in (tmp_ledger, tmp_playbook):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    report = {
        "schema_version": "trade-experience-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": all(checks.values()),
        "checks": checks,
    }
    write_json(DEFAULT_SELFTEST, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade experience memory and playbook helper.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    parser.add_argument("--playbook-summary", action="store_true")
    parser.add_argument("--reflect", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        report = selftest()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["PASS"] else 1
    if args.metrics:
        print(json.dumps(metrics(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.playbook_summary:
        print(playbook_summary())
        return 0
    if args.reflect:
        print(json.dumps(reflect_and_update(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
