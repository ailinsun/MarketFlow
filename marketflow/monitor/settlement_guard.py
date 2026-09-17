"""Settlement guard — pure-function settlement-risk verdicts. No network; every
input is injected by the caller.

The real pain in event-contract venues is not price movement, it is the
settlement mechanism: resolution text that diverges from the headline
(rules-lawyering), an oracle proposal under dispute, and a proposed outcome that
contradicts what the market plainly believes. This module turns those three into
a structured verdict about one held position:

  assess_settlement(pos, meta, uma) → assessment
    phase             : none | window | post_end | proposed | disputed
    rule_gotchas      : sentences in the full resolution text that may diverge
                        from the headline. Verbatim extracts only; when nothing
                        can be extracted the list is empty and nothing is
                        generated to fill it.
    proposed_outcome_* / proposed_vs_held : proposal direction against the
                        direction actually held.
    proposed_side_price / mispriced       : whether the proposed side's market
                        price is meaningfully below 1. A clean settlement
                        converges to ~0.999; divergence is the shape of a
                        mispriced resolution.
    clean / reasons   : whether the settlement mechanism is clean. Fail closed:
                        missing data counts as not clean.
  guard_verdict(assessment) → {action_safe, reasons}
    Semantics: when action_safe is False no automatic action is taken on that
    position at all, only an alert (fail closed). A monitoring-only deployment
    consumes the alert text and nothing else. guard_decision() is the ready-made
    hook for an execution layer.

Grounding rule, hard: every text field is built from real API data — market
metadata, on-chain oracle state, the position itself. Whatever cannot be read
leaves its field empty or None, and the rule layer says so honestly. Nothing is
ever generated to fill a gap.

CANONICAL_UMA_RESOLVERS provenance: marketflow/risk/resolution.py
(the single source of truth lives there; the list comes from enumerating
resolvedBy across the highest-volume markets and covers over 99% of them). This
package holds its own copy so that it imports nothing from the execution stack.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = "marketflow-alerts-settlement-guard-v0.1"

CANONICAL_UMA_RESOLVERS = {
    "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7",  # UMA CTF adapter (binary)
    "0x69c47de9d4d3dad79590d61b9e05918e03775f24",  # NegRisk UMA CTF adapter
    "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d",  # NegRisk CTF adapter
}

# Proposed side priced below this raises a mispriced-settlement warning: a clean
# settlement converges to ~0.999 or better.
DEFAULT_MISPRICED_PRICE_FLOOR = 0.85


# ---------------------------------------------------------------- rule gotchas


# Phrases in resolution text that signal divergence from the headline reading.
# The weight rises the more a phrase looks like an edge case or a fallback.
_GOTCHA_PATTERNS: tuple[tuple[float, re.Pattern[str]], ...] = tuple(
    (weight, re.compile(pat, re.IGNORECASE))
    for weight, pat in (
        (3.0, r"resolve (?:immediately )?to [\"“']?(?:no|yes)\b"),
        (3.0, r"if at any point"),
        (3.0, r"\b50[/-]50\b"),
        (2.5, r"\bpostpon|\bcancel|\babandon|\bsuspend"),
        (2.5, r"resolution source|resolve according to|consensus of credible"),
        (2.0, r"\botherwise\b"),
        (2.0, r"\bsolely\b|\bregardless\b|\bfinal\b"),
        (2.0, r"prior to|before the|by the deadline|deadline of"),
        (1.5, r"official|will not (?:be )?(?:count|qualif|consider)"),
        (1.5, r"in the event|in case of|should the"),
    )
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_MAX_QUOTE_CHARS = 240


def extract_rule_gotchas(question: Any, description: Any, *, max_quotes: int = 2) -> list[str]:
    """Extract sentences from the full resolution text that may diverge from the
    headline reading.

    Heuristic and verbatim: the output is only ever an exact slice of the source
    text, and an empty list when nothing scores. The first sentence is usually a
    restatement of the headline ("This market will resolve to Yes if ..."), so it
    is down-weighted to let fallback and edge-case sentences surface.
    """
    q = str(question or "").strip()
    desc = str(description or "").strip()
    if not desc:
        return []
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(desc) if len(s.strip()) >= 20]
    scored: list[tuple[float, int, str]] = []
    q_words = {w.lower() for w in re.findall(r"[a-z']+", q, re.IGNORECASE) if len(w) > 3}
    for idx, sent in enumerate(sentences):
        score = sum(w for w, pat in _GOTCHA_PATTERNS if pat.search(sent))
        if score <= 0:
            continue
        if idx == 0:
            score -= 1.5  # first sentence restates the headline
        sent_words = {w.lower() for w in re.findall(r"[a-z']+", sent, re.IGNORECASE) if len(w) > 3}
        if q_words and len(sent_words & q_words) / max(len(q_words), 1) > 0.8:
            score -= 1.0  # a near-copy of the headline is not a divergence
        if score > 0:
            scored.append((score, idx, sent))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out: list[str] = []
    for _, _, sent in scored[:max_quotes]:
        out.append(sent if len(sent) <= _MAX_QUOTE_CHARS else sent[: _MAX_QUOTE_CHARS - 1] + "…")
    return out


# ---------------------------------------------------------------- helpers


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _parse_iso(ts: Any) -> Optional[datetime]:
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _held_outcome_index(pos: dict[str, Any], outcomes: list[str]) -> Optional[int]:
    """Index of the held outcome: prefer the reported outcomeIndex, fall back to
    matching the name."""
    idx = pos.get("outcomeIndex")
    try:
        idx = int(idx)
        if 0 <= idx < max(len(outcomes), 2):
            return idx
    except (TypeError, ValueError):
        pass
    held = str(pos.get("outcome") or "").strip().lower()
    if held:
        for i, name in enumerate(outcomes):
            if str(name).strip().lower() == held:
                return i
    return None


# ---------------------------------------------------------------- assessment


def assess_settlement(
    pos: dict[str, Any],
    meta: Optional[dict[str, Any]] = None,
    uma: Optional[dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    window_hours: tuple[float, ...] = (48.0, 24.0),
    final_hours: float = 2.0,
    mispriced_price_floor: float = DEFAULT_MISPRICED_PRICE_FLOOR,
) -> dict[str, Any]:
    """Settlement-risk verdict for one position. `meta` is market metadata (may
    be None) and `uma` is on-chain proposal state (may be None). Pure function."""
    now = now or datetime.now(timezone.utc)
    meta = meta if isinstance(meta, dict) else {}
    uma = uma if isinstance(uma, dict) else None

    outcomes = [str(x).strip() for x in _parse_json_list(meta.get("outcomes"))]
    outcome_prices = [_to_float(x) for x in _parse_json_list(meta.get("outcomePrices"))]
    statuses = {str(x).strip().lower() for x in _parse_json_list(meta.get("umaResolutionStatuses"))}
    resolver = str(meta.get("resolvedBy") or "").strip().lower()

    end_dt = _parse_iso(pos.get("endDate") or meta.get("endDate"))
    hours_to_end = (end_dt - now).total_seconds() / 3600.0 if end_dt else None

    # On-chain UMA state is AUTHORITATIVE when we could read it — gamma's
    # `umaResolutionStatuses` lags (it can still say ["proposed"] minutes after the
    # challenge window closed and the proposal settled on-chain). Only fall back to
    # gamma's statuses when the chain read failed. This avoids false "proposed /
    # mispriced" alerts on a market that already settled cleanly.
    onchain_state = uma.get("state_label") if uma else None
    if uma is not None:
        disputed = bool(uma.get("disputed")) or onchain_state == "disputed"
        # active challenge window only while genuinely proposed / expired-unsettled
        proposed = onchain_state in ("proposed", "expired") and not disputed
    else:
        disputed = any("disput" in s for s in statuses)
        proposed = any("propos" in s for s in statuses)

    if disputed:
        phase = "disputed"
    elif proposed:
        phase = "proposed"
    elif hours_to_end is not None and hours_to_end <= 0 and not pos.get("redeemable"):
        phase = "post_end"
    elif hours_to_end is not None and 0 < hours_to_end <= max(window_hours):
        phase = "window"
    else:
        phase = "none"

    # Window buckets: 48h -> 1, 24h -> 2, 2h -> 3. Tighter is higher, and the
    # de-duplication layer only re-alerts when the bucket worsens.
    window_bucket = 0
    if hours_to_end is not None and hours_to_end > 0:
        for i, h in enumerate(sorted(window_hours, reverse=True)):
            if hours_to_end <= h:
                window_bucket = i + 1
        if hours_to_end <= final_hours:
            window_bucket = len(window_hours) + 1

    rule_gotchas = extract_rule_gotchas(
        meta.get("question") or pos.get("title"), meta.get("description")
    )

    # Proposal direction against held direction. Every field comes from real
    # data; whatever is missing stays None.
    proposed_index = uma.get("proposed_outcome_index") if uma else None
    proposed_label: Optional[str] = None
    if proposed_index is not None and proposed_index < len(outcomes):
        proposed_label = outcomes[proposed_index]
    elif uma and uma.get("proposed_outcome_kind") == "split_50_50":
        proposed_label = "50/50 (unresolvable)"

    held_index = _held_outcome_index(pos, outcomes)
    proposed_vs_held: Optional[str] = None
    if proposed_index is not None and held_index is not None:
        proposed_vs_held = "with_you" if proposed_index == held_index else "against_you"

    proposed_side_price: Optional[float] = None
    if proposed_index is not None and proposed_index < len(outcome_prices):
        proposed_side_price = outcome_prices[proposed_index]

    mispriced = (
        phase in ("proposed", "disputed")
        and proposed_side_price is not None
        and proposed_side_price < mispriced_price_floor
    )

    # Settlement cleanliness, fail closed: clean is only reachable with metadata
    # present, and missing data counts as not clean.
    reasons: list[str] = []
    if not meta:
        reasons.append("no_market_metadata")
    else:
        if resolver and resolver not in CANONICAL_UMA_RESOLVERS:
            reasons.append(f"non_standard_uma_resolver:{resolver}")
        elif not resolver:
            reasons.append("missing_resolver")
    if disputed:
        reasons.append("uma_disputed")
    if mispriced:
        reasons.append(f"proposed_outcome_mispriced:{proposed_side_price}")
    if uma and uma.get("proposed_outcome_kind") in ("split_50_50", "unknown"):
        reasons.append(f"proposal_{uma['proposed_outcome_kind']}")
    if phase == "post_end" and hours_to_end is not None and hours_to_end < -24.0:
        reasons.append("unresolved_24h_past_end")

    return {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "hours_to_end": round(hours_to_end, 2) if hours_to_end is not None else None,
        "window_bucket": window_bucket,
        "rule_gotchas": rule_gotchas,
        "statuses": sorted(statuses),
        "uma_onchain": uma,
        "proposed_outcome_index": proposed_index,
        "proposed_outcome_label": proposed_label,
        "held_outcome_index": held_index,
        "proposed_vs_held": proposed_vs_held,
        "proposed_side_price": proposed_side_price,
        "mispriced": mispriced,
        "clean": not reasons,
        "reasons": reasons,
    }


# ---------------------------------------------------------------- guard


def guard_verdict(assessment: dict[str, Any]) -> dict[str, Any]:
    """Guard semantics: an unclean settlement means no action (fail closed),
    only an alert."""
    reasons = list(assessment.get("reasons") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "action_safe": not reasons,
        "reasons": reasons,
        "phase": assessment.get("phase"),
    }


def guard_decision(decision: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """Execution-layer hook: an actionable decision plus an unclean verdict is
    rewritten fail-closed.

    A clean verdict (action_safe), or a decision that is not an action, is
    returned unchanged. A rewrite keeps the suppressed decision for the audit
    trail and states, in the reason, why the guard stopped it.
    """
    if verdict.get("action_safe"):
        return decision
    reasons = verdict.get("reasons") or ["settlement_not_clean"]
    return {
        **decision,
        "decision": "SETTLEMENT_GUARD_HOLD",
        "suppressed_decision": decision.get("decision"),
        "rule_fired": "settlement_guard",
        "reason": (
            "settlement guard: market settlement is not clean "
            f"({', '.join(str(r) for r in reasons)}); holding all automated action "
            f"(suppressed: {decision.get('decision')}). Review this position manually."
        ),
        "settlement_guard": verdict,
    }


# ---------------------------------------------------------------- selftest


def selftest() -> dict[str, Any]:
    from datetime import timedelta

    checks: dict[str, bool] = {}
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)

    # Against a real resolution text, extraction must hit the early-elimination
    # fallback clause, and the output must be a verbatim slice of the source.
    usa_desc = (
        "This market will resolve according to the national team that wins the "
        "2026 FIFA World Cup.\n\nIf at any point it becomes impossible for this "
        "team to win the FIFA World Cup based on the rules of FIFA (e.g., they "
        "are eliminated in the knockout stage), this market will resolve "
        "immediately to “No”."
    )
    gotchas = extract_rule_gotchas("Will USA win the 2026 FIFA World Cup?", usa_desc)
    checks["gotcha_hits_fallback"] = any("resolve" in g.lower() and "immediately" in g.lower() for g in gotchas)
    checks["gotcha_verbatim"] = all(g.rstrip("…") in usa_desc for g in gotchas)
    checks["gotcha_empty_on_no_desc"] = extract_rule_gotchas("q", "") == []

    def pos(**kw):
        base = {"outcome": "Yes", "outcomeIndex": 0, "endDate": "", "redeemable": False, "title": "t"}
        base.update(kw)
        return base

    def meta(**kw):
        base = {
            "question": "Will X happen?",
            "description": usa_desc,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.97", "0.03"]',
            "umaResolutionStatuses": "[]",
            "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7",
            "endDate": "",
        }
        base.update(kw)
        return base

    # window buckets
    e48 = (now + timedelta(hours=40)).isoformat()
    a = assess_settlement(pos(endDate=e48), meta(), now=now)
    checks["window_48h_bucket1"] = a["phase"] == "window" and a["window_bucket"] == 1
    e24 = (now + timedelta(hours=10)).isoformat()
    a = assess_settlement(pos(endDate=e24), meta(), now=now)
    checks["window_24h_bucket2"] = a["window_bucket"] == 2
    e2 = (now + timedelta(hours=1)).isoformat()
    a = assess_settlement(pos(endDate=e2), meta(), now=now)
    checks["window_2h_bucket3"] = a["window_bucket"] == 3
    checks["window_carries_gotchas"] = bool(a["rule_gotchas"])

    # proposal direction against the held side
    uma_prop = {"state_label": "proposed", "proposed_outcome_index": 0,
                "proposed_outcome_kind": "outcome0", "disputed": False}
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]'), uma_prop, now=now)
    checks["proposed_with_you"] = a["phase"] == "proposed" and a["proposed_vs_held"] == "with_you"
    checks["proposed_label_real_outcome"] = a["proposed_outcome_label"] == "Yes"
    checks["proposed_clean_when_aligned"] = a["clean"] is True

    a = assess_settlement(pos(outcome="No", outcomeIndex=1), meta(umaResolutionStatuses='["proposed"]'), uma_prop, now=now)
    checks["proposed_against_you"] = a["proposed_vs_held"] == "against_you"

    # direction unreadable on-chain -> honest None, never an invented direction
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]'), None, now=now)
    checks["no_onchain_no_direction"] = (
        a["phase"] == "proposed" and a["proposed_outcome_label"] is None
        and a["proposed_vs_held"] is None and a["mispriced"] is False
    )

    # mispriced settlement: Yes proposed but the market only pays 0.55
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]', outcomePrices='["0.55", "0.45"]'), uma_prop, now=now)
    checks["mispriced_flags"] = a["mispriced"] is True and not a["clean"]
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]', outcomePrices='["0.99", "0.01"]'), uma_prop, now=now)
    checks["converged_not_mispriced"] = a["mispriced"] is False

    # disputed: the metadata source alone must trigger it too
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed", "disputed"]'), None, now=now)
    checks["disputed_phase"] = a["phase"] == "disputed" and "uma_disputed" in a["reasons"]

    # On-chain state overrides a lagging metadata API: metadata still says
    # proposed while the chain shows settled and undisputed, so it must not be
    # treated as proposed and must not report a false mispricing.
    uma_settled = {"state_label": "settled", "proposed_outcome_index": 0,
                   "proposed_outcome_kind": "outcome0", "disputed": False, "settled": True}
    a = assess_settlement(
        pos(endDate=(now - timedelta(hours=1)).isoformat()),
        meta(umaResolutionStatuses='["proposed"]', outcomePrices='["0.50", "0.50"]'),
        uma_settled, now=now)
    checks["onchain_settled_overrides_gamma_lag"] = (
        a["phase"] != "proposed" and a["mispriced"] is False
    )
    # but a real on-chain dispute upgrades a metadata "proposed" to disputed
    uma_disp = {"state_label": "disputed", "proposed_outcome_index": 0,
                "proposed_outcome_kind": "outcome0", "disputed": True}
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]'), uma_disp, now=now)
    checks["onchain_dispute_upgrades_phase"] = a["phase"] == "disputed"

    # non-standard resolver -> not clean
    a = assess_settlement(pos(), meta(resolvedBy="0x" + "de" * 20), None, now=now)
    checks["nonstandard_resolver_dirty"] = not a["clean"] and any("non_standard" in r for r in a["reasons"])

    # missing metadata -> fail closed, not clean, but the phase is still derived
    # from the position's own end date
    a = assess_settlement(pos(endDate=e24), None, None, now=now)
    checks["no_meta_fail_closed"] = not a["clean"] and a["phase"] == "window"

    # a 50/50 proposal -> not clean
    uma_split = {"state_label": "proposed", "proposed_outcome_index": None,
                 "proposed_outcome_kind": "split_50_50", "disputed": False}
    a = assess_settlement(pos(), meta(umaResolutionStatuses='["proposed"]'), uma_split, now=now)
    checks["split_50_50_dirty"] = not a["clean"] and a["proposed_outcome_label"] == "50/50 (unresolvable)"

    # guard verdict plus the execution-layer hook
    dirty = assess_settlement(pos(), meta(umaResolutionStatuses='["disputed"]'), None, now=now)
    v = guard_verdict(dirty)
    checks["verdict_fail_closed"] = v["action_safe"] is False and "uma_disputed" in v["reasons"]
    held = guard_decision({"decision": "TAKE_PROFIT_SELL", "reason": "x"}, v)
    checks["guard_holds_action"] = (
        held["decision"] == "SETTLEMENT_GUARD_HOLD"
        and held["suppressed_decision"] == "TAKE_PROFIT_SELL"
    )
    clean = assess_settlement(pos(endDate=e48), meta(), now=now)
    passed = guard_decision({"decision": "TAKE_PROFIT_SELL"}, guard_verdict(clean))
    checks["guard_passes_clean"] = passed["decision"] == "TAKE_PROFIT_SELL"

    ok = all(checks.values())
    return {"schema_version": SCHEMA_VERSION + "-selftest", "PASS": ok,
            "checks": checks, "failed": [k for k, v in checks.items() if not v]}


if __name__ == "__main__":
    import json as _json

    rep = selftest()
    print(_json.dumps(rep, ensure_ascii=False, indent=2))
    raise SystemExit(0 if rep["PASS"] else 1)
