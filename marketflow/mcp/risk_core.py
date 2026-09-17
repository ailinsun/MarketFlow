#!/usr/bin/env python3
"""Deterministic event-contract and portfolio risk primitives.

Venue adapters supply public positions, contract metadata and order books. This
module owns canonical identifiers, component risk, rule versions, liquidation
math and append-only audit/event records. It never places orders.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


ENGINE_VERSION = "event-risk-v0.1.0"
SCHEMA_VERSION = "marketflow-event-risk-v1"
LEVELS = ("low", "medium", "high", "critical", "unknown")
LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SAFETY_CRITICAL = {"resolution_state", "dispute_risk", "liquidity_exit", "data_freshness"}
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def utc_now(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
    try:
        numeric = float(str(value).strip())
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else None
    except (TypeError, ValueError):
        return None


def list_value(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_contract_id(meta: dict | None, position: dict | None = None) -> str:
    meta, position = meta or {}, position or {}
    venue_id = (meta.get("conditionId") or meta.get("condition_id")
                or position.get("conditionId") or position.get("condition_id")
                or meta.get("slug") or position.get("slug") or "unknown")
    return "marketflow:polymarket:" + str(venue_id).strip().lower()


def canonical_event_id(meta: dict | None, position: dict | None = None) -> str:
    meta, position = meta or {}, position or {}
    source = (meta.get("eventSlug") or position.get("eventSlug") or position.get("event_slug")
              or meta.get("groupItemTitle") or meta.get("question") or position.get("title")
              or canonical_contract_id(meta, position))
    return "marketflow:event:" + sha256_text(str(source).strip().lower())[:24]


def evidence(source: str, source_id: Any, observed_at: str, *, upstream_at: Any = None,
             url: str | None = None, detail: str | None = None) -> dict:
    out = {"source": source, "source_id": source_id, "observed_at": observed_at}
    if upstream_at is not None:
        out["published_at"] = upstream_at
    if url:
        out["reference"] = url
    if detail:
        out["detail"] = detail
    return out


def component(name: str, level: str, reasons: list[str], evidences: list[dict],
              observed_at: str, freshness: str, score: float | None = None) -> dict:
    if level not in LEVELS:
        raise ValueError(f"invalid risk level: {level}")
    out = {
        "component": name,
        "level": level,
        "reasons": reasons,
        "evidence": evidences,
        "observed_at": observed_at,
        "data_freshness": freshness,
        "engine_version": ENGINE_VERSION,
    }
    if score is not None:
        out["score"] = score
    return out


def aggregate(components: list[dict]) -> tuple[str, list[str]]:
    critical = [c["component"] for c in components if c.get("level") == "critical"]
    if critical:
        return "critical", ["critical component: " + x for x in critical]
    unknown = [c["component"] for c in components
               if c.get("level") == "unknown" and c.get("component") in SAFETY_CRITICAL]
    if unknown:
        return "unknown", ["safety-relevant data unavailable: " + x for x in unknown]
    known = [c for c in components if c.get("level") in LEVEL_RANK]
    if not known:
        return "unknown", ["no known risk components"]
    level = max(known, key=lambda c: LEVEL_RANK[c["level"]])["level"]
    drivers = [c["component"] for c in known if c["level"] == level]
    return level, [f"highest deterministic component band is {level}: " + ", ".join(drivers)]


def normalized_resolution_state(meta: dict, position: dict, *, now: float | None = None) -> tuple[str, str | None]:
    statuses = {str(x).strip().lower() for x in list_value(meta.get("umaResolutionStatuses"))}
    raw = ",".join(sorted(statuses)) or None
    if any("disput" in s for s in statuses):
        return "DISPUTED", raw
    if any("propos" in s for s in statuses):
        return "DISPUTE_WINDOW", raw
    if position.get("redeemable"):
        return "REDEEMABLE", raw
    if bool(meta.get("closed")):
        prices = [number(x) for x in list_value(meta.get("outcomePrices"))]
        if any(x in (0.0, 1.0) for x in prices):
            return "RESOLVED", raw
        return "SETTLEMENT_PENDING", raw
    end = parse_time(meta.get("endDate") or position.get("endDate"))
    if end is not None and end <= (now if now is not None else time.time()):
        return "ELIGIBLE_FOR_RESOLUTION", raw
    if bool(meta.get("active", True)):
        return "OPEN", raw
    return "CLOSED", raw


def liquidation_scenarios(size: float | None, mark: float | None, bids: list[dict] | None) -> list[dict]:
    if size is None or size <= 0:
        return []
    clean: list[tuple[float, float]] = []
    for row in bids or []:
        p, q = number(row.get("price")), number(row.get("size"))
        if p is not None and q is not None and 0 <= p <= 1 and q > 0:
            clean.append((p, q))
    clean.sort(reverse=True)
    out = []
    for share in (0.25, 0.50, 1.00):
        requested = size * share
        remaining, proceeds, executable = requested, 0.0, 0.0
        for price, available in clean:
            take = min(remaining, available)
            proceeds += take * price
            executable += take
            remaining -= take
            if remaining <= 1e-12:
                break
        avg = proceeds / executable if executable else None
        slippage = ((mark - avg) / mark) if mark and avg is not None and mark > 0 else None
        impact = ((mark * executable) - proceeds) if mark is not None else None
        out.append({
            "fraction": share,
            "requested_size": round(requested, 6),
            "executable_size": round(executable, 6),
            "available_executable_depth": round(executable, 6),
            "estimated_average_price": round(avg, 6) if avg is not None else None,
            "estimated_slippage_pct": round(slippage * 100, 4) if slippage is not None else None,
            "estimated_dollar_impact": round(impact, 2) if impact is not None else None,
            "insufficient_depth": remaining > 1e-9,
        })
    return out


def liquidity_component(scenarios: list[dict], observed_at: str, ev: list[dict]) -> dict:
    if not scenarios:
        return component("liquidity_exit", "unknown", ["order-book depth unavailable"], ev,
                         observed_at, "unknown")
    full = scenarios[-1]
    if full["insufficient_depth"]:
        return component("liquidity_exit", "critical",
                         ["full exit exceeds currently executable bid depth"], ev,
                         observed_at, "fresh")
    slip = full.get("estimated_slippage_pct")
    if slip is None:
        return component("liquidity_exit", "unknown", ["slippage cannot be computed"], ev,
                         observed_at, "unknown")
    if slip >= 10:
        level = "high"
    elif slip >= 3:
        level = "medium"
    else:
        level = "low"
    return component("liquidity_exit", level,
                     [f"estimated full-exit slippage is {slip:.2f}%"], ev,
                     observed_at, "fresh", score=round(slip, 4))


class AppendOnlyStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    @staticmethod
    def _append(path: str, row: dict) -> None:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        body = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, body)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _rows(path: str) -> list[dict]:
        out = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        out.append(row)
        except OSError:
            pass
        return out

    def observe_rules(self, contract_id: str, rules: str, observed_at: str) -> dict:
        normalized = "\n".join(line.rstrip() for line in (rules or "").strip().splitlines())
        if not normalized:
            return {"version": None, "hash": None, "changed": None, "diff": []}
        digest = sha256_text(normalized)
        key = sha256_text(contract_id)[:32]
        path = self._path(os.path.join("rules", key + ".jsonl"))
        previous = self._rows(path)
        last = previous[-1] if previous else None
        if last and last.get("hash") == digest:
            return {"version": last["version"], "hash": digest, "changed": False, "diff": []}
        diff = []
        if last:
            diff = list(difflib.unified_diff(
                str(last.get("rules") or "").splitlines(), normalized.splitlines(), lineterm=""))[:80]
        version = f"rules-{digest[:16]}"
        self._append(path, {"contract_id": contract_id, "version": version, "hash": digest,
                            "rules": normalized, "observed_at": observed_at})
        changed = True if last else None
        if changed:
            self.emit("contract.rules_changed", contract_id,
                      {"previous_version": last.get("version"), "rules_version": version,
                       "diff": diff}, observed_at)
        return {"version": version, "hash": digest, "changed": changed, "diff": diff}

    def emit(self, event_type: str, contract_id: str | None, payload: dict,
             observed_at: str) -> dict:
        stable = json.dumps([event_type, contract_id, payload, observed_at], sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
        row = {"event_id": "evt_" + sha256_text(stable)[:24], "type": event_type,
               "contract_id": contract_id, "observed_at": observed_at, "payload": payload,
               "engine_version": ENGINE_VERSION}
        known = {x.get("event_id") for x in self._rows(self._path("risk_events.jsonl"))[-5000:]}
        if row["event_id"] not in known:
            self._append(self._path("risk_events.jsonl"), row)
            self.enqueue_webhooks(row)
        return row

    def observe_risk(self, result: dict) -> None:
        contract_id = result.get("marketflow_contract_id")
        if not contract_id:
            return
        key = sha256_text(contract_id)[:32]
        path = self._path(os.path.join("risk_states", key + ".jsonl"))
        rows = self._rows(path)
        last = rows[-1] if rows else None
        state = {"contract_id": contract_id, "overall_risk_band": result.get("overall_risk_band"),
                 "resolution_state": result.get("resolution", {}).get("canonical_state"),
                 "rules_version": result.get("rules", {}).get("version"),
                 "observed_at": result.get("observed_at"), "engine_version": ENGINE_VERSION}
        if last and all(last.get(k) == state.get(k) for k in
                        ("overall_risk_band", "resolution_state", "rules_version")):
            return
        self._append(path, state)
        if last and last.get("overall_risk_band") != state["overall_risk_band"]:
            self.emit("risk.level_changed", contract_id,
                      {"from": last.get("overall_risk_band"), "to": state["overall_risk_band"]},
                      state["observed_at"])
        if last and last.get("resolution_state") != state["resolution_state"]:
            self.emit("resolution.state_changed", contract_id,
                      {"from": last.get("resolution_state"), "to": state["resolution_state"]},
                      state["observed_at"])

    def decision(self, wallet: str, result: dict) -> str:
        decision_id = "dec_" + uuid.uuid4().hex
        components = {c["component"]: c["level"] for c in result.get("risk_components", [])}
        self._append(self._path("decisions.jsonl"), {
            "decision_id": decision_id,
            "portfolio_ref": sha256_text(wallet.lower())[:24],
            "contract_id": result.get("marketflow_contract_id"),
            "rules_version": result.get("rules", {}).get("version"),
            "risk_engine_version": ENGINE_VERSION,
            "components": components,
            "final_decision": result.get("overall_risk_band"),
            "reasons": result.get("risk_reasons", []),
            "decision_timestamp": result.get("observed_at"),
        })
        return decision_id

    def feed(self, since: float | None = None, limit: int = 100) -> dict:
        rows = self._rows(self._path("risk_events.jsonl"))
        if since is not None:
            rows = [r for r in rows if (parse_time(r.get("observed_at")) or 0) > since]
        rows = rows[-max(1, min(limit, 500)):]
        cursor = rows[-1]["observed_at"] if rows else utc_now(since or 0)
        return {"schema": SCHEMA_VERSION, "events": rows, "next_cursor": cursor,
                "engine_version": ENGINE_VERSION}

    def enqueue_webhooks(self, event: dict) -> None:
        for endpoint in load_webhook_config(self._path("webhooks.json")):
            delivery_id = "wh_" + sha256_text(endpoint["id"] + ":" + event["event_id"])[:24]
            existing = {r.get("delivery_id") for r in self._rows(self._path("webhook_deliveries.jsonl"))}
            if delivery_id in existing:
                continue
            self._append(self._path("webhook_deliveries.jsonl"), {
                "delivery_id": delivery_id, "endpoint_id": endpoint["id"],
                "event": event, "status": "queued", "attempt": 0,
                "next_attempt_at": time.time(), "created_at": utc_now(),
            })


def contract_risk(position: dict, meta: dict | None, book: dict | None, *, store: AppendOnlyStore,
                  gotchas: Callable[[Any, Any, int], list] | None = None,
                  now: float | None = None) -> dict:
    ts = now if now is not None else time.time()
    observed_at = utc_now(ts)
    meta = meta or {}
    cid = canonical_contract_id(meta, position)
    eid = canonical_event_id(meta, position)
    rules_text = str(meta.get("description") or "").strip()
    rule_state = store.observe_rules(cid, rules_text, observed_at)
    gotcha_rows = gotchas(meta.get("question"), rules_text, max_quotes=3) if gotchas else []
    position_ev = evidence("polymarket-data-api", position.get("asset"), observed_at,
                           upstream_at=position.get("updatedAt"))
    meta_ev = evidence("polymarket-gamma", meta.get("conditionId") or meta.get("slug"), observed_at,
                       upstream_at=meta.get("updatedAt"),
                       url=("https://polymarket.com/event/" + str(position.get("eventSlug")))
                       if position.get("eventSlug") else None)
    book_ev = evidence("polymarket-clob", position.get("asset"), observed_at,
                       upstream_at=(book or {}).get("timestamp"))

    if not rules_text:
        rule = component("rule_clarity", "unknown", ["operative market rules unavailable"],
                         [meta_ev], observed_at, "unknown")
    elif gotcha_rows:
        rule = component("rule_clarity", "medium",
                         ["rules contain non-obvious or conditional clauses"], [meta_ev],
                         observed_at, "fresh")
    else:
        rule = component("rule_clarity", "low",
                         ["no non-obvious clause detected by deterministic checks"], [meta_ev],
                         observed_at, "fresh")

    state, raw_state = normalized_resolution_state(meta, position, now=ts)
    if not meta:
        resolution = component("resolution_state", "unknown", ["market metadata unavailable"],
                               [position_ev], observed_at, "unknown")
    elif state == "DISPUTED":
        resolution = component("resolution_state", "critical", ["resolution is disputed"],
                               [meta_ev], observed_at, "fresh")
    elif state in ("DISPUTE_WINDOW", "SETTLEMENT_PENDING"):
        resolution = component("resolution_state", "high", [f"contract is in {state}"],
                               [meta_ev], observed_at, "fresh")
    elif state == "ELIGIBLE_FOR_RESOLUTION":
        resolution = component("resolution_state", "medium",
                               ["contract is eligible for resolution but not resolved"], [meta_ev],
                               observed_at, "fresh")
    else:
        resolution = component("resolution_state", "low", [f"canonical state is {state}"],
                               [meta_ev], observed_at, "fresh")

    source = meta.get("resolutionSource") or meta.get("resolutionSourceUrl")
    source_comp = component(
        "source_dependency", "low" if source else "unknown",
        ["designated resolution source is explicit" if source
         else "designated resolution source is not structured in upstream metadata"],
        [meta_ev], observed_at, "fresh" if source else "unknown")
    if state == "DISPUTED":
        dispute = component("dispute_risk", "critical", ["active dispute observed"], [meta_ev],
                            observed_at, "fresh")
    elif state == "DISPUTE_WINDOW":
        dispute = component("dispute_risk", "high", ["proposal/challenge window may be active"],
                            [meta_ev], observed_at, "fresh")
    elif meta:
        dispute = component("dispute_risk", "low", ["no dispute flag observed"], [meta_ev],
                            observed_at, "fresh")
    else:
        dispute = component("dispute_risk", "unknown", ["dispute state unavailable"], [position_ev],
                            observed_at, "unknown")
    changed = rule_state["changed"]
    rules_change = component(
        "rules_change", "high" if changed is True else "low" if changed is False else "unknown",
        ["operative rules changed since the previous observation" if changed is True else
         "rules match the previous observed version" if changed is False else
         "first observation cannot establish whether rules changed earlier"],
        [meta_ev], observed_at, "fresh" if changed is not None else "unknown")

    size, mark = number(position.get("size")), number(position.get("curPrice"))
    scenarios = liquidation_scenarios(size, mark, (book or {}).get("bids"))
    liquidity = liquidity_component(scenarios, observed_at, [book_ev])
    end_ts = parse_time(meta.get("endDate") or position.get("endDate"))
    if end_ts is None:
        timing = component("time_to_resolution", "unknown", ["resolution deadline unavailable"],
                           [meta_ev], observed_at, "unknown")
    else:
        hours = (end_ts - ts) / 3600
        level = "high" if hours <= 24 else "medium" if hours <= 168 else "low"
        timing = component("time_to_resolution", level,
                           [f"scheduled end is {hours:.1f} hours from observation"], [meta_ev],
                           observed_at, "fresh", score=round(hours, 2))
    if state in ("DISPUTED", "DISPUTE_WINDOW", "SETTLEMENT_PENDING"):
        lockup = component("capital_lockup", "high", [f"capital is exposed during {state}"],
                           [meta_ev], observed_at, "fresh")
    elif state == "RESOLVED" and not position.get("redeemable"):
        lockup = component("capital_lockup", "high",
                           ["contract appears resolved but position is not redeemable"], [meta_ev],
                           observed_at, "fresh")
    else:
        lockup = component("capital_lockup", "low", ["normal open or redeemable state"],
                           [meta_ev], observed_at, "fresh")
    meta_upstream = parse_time(meta.get("updatedAt"))
    book_upstream = parse_time((book or {}).get("timestamp"))
    if not meta or not book:
        fresh = component("data_freshness", "unknown",
                          ["one or more safety-relevant upstream datasets are unavailable"],
                          [position_ev, meta_ev, book_ev], observed_at, "unknown")
    elif book_upstream is not None and ts - book_upstream > 300:
        fresh = component("data_freshness", "high", ["order book timestamp is older than 5 minutes"],
                          [book_ev], observed_at, "stale", score=round(ts - book_upstream, 1))
    else:
        reason = "all required upstream reads completed during this request"
        if meta_upstream is None or book_upstream is None:
            reason += "; one or more upstream publish timestamps are absent"
        fresh = component("data_freshness", "low", [reason],
                          [position_ev, meta_ev, book_ev], observed_at, "fresh")

    components = [rule, resolution, source_comp, dispute, rules_change, liquidity, timing,
                  lockup, fresh]
    band, risk_reasons = aggregate(components)
    current_value = number(position.get("currentValue"))
    if current_value is None and size is not None and mark is not None:
        current_value = size * mark
    initial_value = number(position.get("initialValue"))
    result = {
        "schema": SCHEMA_VERSION,
        "marketflow_event_id": eid,
        "marketflow_contract_id": cid,
        "venue": "polymarket",
        "venue_market_id": meta.get("id"),
        "venue_condition_id": meta.get("conditionId") or position.get("conditionId"),
        "market_title": meta.get("question") or position.get("title"),
        "market_slug": meta.get("slug") or position.get("slug"),
        "category": meta.get("category") or position.get("category") or "unknown",
        "outcome": position.get("outcome"),
        "outcome_index": position.get("outcomeIndex"),
        "token_id": position.get("asset"),
        "position": {
            "size": size,
            "average_entry_price": number(position.get("avgPrice")),
            "current_market_price": mark,
            "estimated_current_value": current_value,
            "cost_basis": initial_value,
            "maximum_loss_from_now": current_value,
            "potential_payout": size,
            "realized_pnl": number(position.get("realizedPnl")),
            "unrealized_pnl": number(position.get("cashPnl")),
            "redeemable": bool(position.get("redeemable")),
        },
        "resolution": {
            "canonical_state": state,
            "raw_venue_state": raw_state,
            "scheduled_end": meta.get("endDate") or position.get("endDate"),
            "designated_source": source,
            "resolver": meta.get("resolvedBy") or "uma_optimistic_oracle",
            "dispute_window_remaining_sec": None,
        },
        "rules": {"version": rule_state["version"], "hash": rule_state["hash"],
                  "changed": rule_state["changed"], "diff": rule_state["diff"],
                  "gotchas": gotcha_rows},
        "liquidity": {"book_timestamp": (book or {}).get("timestamp"),
                      "exit_scenarios": scenarios},
        "risk_components": components,
        "overall_risk_band": band,
        "risk_reasons": risk_reasons,
        "provenance": [position_ev, meta_ev, book_ev],
        "observed_at": observed_at,
        "risk_engine_version": ENGINE_VERSION,
    }
    store.observe_risk(result)
    return result


def portfolio_rollup(wallet: str, positions: list[dict], risks: list[dict], *, now: float | None = None,
                     store: AppendOnlyStore | None = None) -> dict:
    observed_at = utc_now(now)
    total = sum(number(r.get("position", {}).get("estimated_current_value")) or 0 for r in risks)
    payout = sum(number(r.get("position", {}).get("potential_payout")) or 0 for r in risks)
    cost = sum(number(r.get("position", {}).get("cost_basis")) or 0 for r in risks)
    resolving24 = resolving7 = elevated = stale = 0.0
    by_event: dict[str, float] = {}
    by_category: dict[str, float] = {}
    by_source: dict[str, float] = {}
    by_resolver: dict[str, float] = {}
    for r in risks:
        v = number(r["position"].get("estimated_current_value")) or 0
        by_event[r["marketflow_event_id"]] = by_event.get(r["marketflow_event_id"], 0) + v
        for bucket, key in ((by_category, r.get("category") or "unknown"),
                            (by_source, r["resolution"].get("designated_source") or "unknown"),
                            (by_resolver, r["resolution"].get("resolver") or "unknown")):
            bucket[str(key)] = bucket.get(str(key), 0) + v
        end = parse_time(r["resolution"].get("scheduled_end"))
        if end is not None and now is not None:
            if end <= now + 86400:
                resolving24 += v
            if end <= now + 7 * 86400:
                resolving7 += v
        if r["overall_risk_band"] in ("high", "critical"):
            elevated += v
        if any(c["component"] == "data_freshness" and c["level"] in ("unknown", "high", "critical")
               for c in r["risk_components"]):
            stale += v

    concentration = []
    for r in risks:
        v = number(r["position"].get("estimated_current_value")) or 0
        share = v / total if total > 0 else None
        level = "unknown" if share is None else "high" if share > .50 else "medium" if share > .25 else "low"
        c = component("portfolio_concentration", level,
                      ["portfolio share unavailable" if share is None else
                       f"position is {share * 100:.2f}% of current portfolio value"],
                      r["provenance"][:1], observed_at,
                      "unknown" if share is None else "fresh",
                      round(share * 100, 4) if share is not None else None)
        r["risk_components"].append(c)
        r["overall_risk_band"], r["risk_reasons"] = aggregate(r["risk_components"])
        if store:
            r["decision_id"] = store.decision(wallet, r)
        concentration.append(c)

    def shares(rows: dict[str, float]) -> list[dict]:
        return [{"key": k, "value_usd": round(v, 2),
                 "share_pct": round(v / total * 100, 2) if total else None}
                for k, v in sorted(rows.items(), key=lambda kv: kv[1], reverse=True)]

    bands: dict[str, int] = {}
    for r in risks:
        bands[r["overall_risk_band"]] = bands.get(r["overall_risk_band"], 0) + 1
    return {
        "schema": SCHEMA_VERSION,
        "portfolio": {"venue": "polymarket", "wallet": wallet,
                      "read_only": True, "custody_required": False},
        "summary": {
            "total_current_exposure": round(total, 2),
            "maximum_loss_from_now": round(total, 2),
            "maximum_payout": round(payout, 2),
            "cost_basis": round(cost, 2),
            "active_positions": len(risks),
            "capital_resolving_within_24h": round(resolving24, 2),
            "capital_resolving_within_7d": round(resolving7, 2),
            "capital_in_elevated_settlement_state": round(elevated, 2),
            "capital_with_stale_or_incomplete_evidence": round(stale, 2),
            "risk_band_counts": bands,
        },
        "concentration": {"by_event": shares(by_event), "by_category": shares(by_category),
                          "by_resolution_source": shares(by_source),
                          "by_resolver": shares(by_resolver)},
        "positions": risks,
        "observed_at": observed_at,
        "risk_engine_version": ENGINE_VERSION,
        "provenance": {"positions": "polymarket-data-api", "contracts": "polymarket-gamma",
                       "liquidity": "polymarket-clob"},
    }


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body,
                             hashlib.sha256).hexdigest()


def load_webhook_config(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = doc.get("endpoints") if isinstance(doc, dict) else None
    out = []
    for row in rows or []:
        if (isinstance(row, dict) and row.get("enabled", True) and row.get("id")
                and str(row.get("url", "")).startswith("https://") and row.get("secret")):
            out.append({"id": str(row["id"]), "url": str(row["url"]),
                        "secret": str(row["secret"])})
    return out


class WebhookWorker:
    def __init__(self, store: AppendOnlyStore, *, opener=None):
        self.store = store
        self.opener = opener or urllib.request.urlopen
        self.stop = threading.Event()

    def run_once(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        path = self.store._path("webhook_deliveries.jsonl")
        rows = self.store._rows(path)
        latest: dict[str, dict] = {}
        for row in rows:
            latest[str(row.get("delivery_id"))] = row
        configs = {x["id"]: x for x in load_webhook_config(self.store._path("webhooks.json"))}
        delivered = 0
        for delivery_id, row in latest.items():
            if row.get("status") in ("delivered", "disabled") or float(row.get("next_attempt_at") or 0) > now:
                continue
            endpoint = configs.get(str(row.get("endpoint_id")))
            if not endpoint:
                continue
            body = json.dumps(row["event"], ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode()
            stamp = utc_now(now)
            req = urllib.request.Request(endpoint["url"], data=body, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "marketflow-risk-webhook/0.1",
                "X-MarketFlow-Delivery": delivery_id, "X-MarketFlow-Timestamp": stamp,
                "X-MarketFlow-Signature": webhook_signature(endpoint["secret"], stamp, body),
            })
            attempt = int(row.get("attempt") or 0) + 1
            try:
                with self.opener(req, timeout=10) as response:
                    ok = 200 <= int(getattr(response, "status", 0)) < 300
            except Exception:
                ok = False
            if ok:
                status, next_at = "delivered", None
                delivered += 1
            elif attempt >= 8:
                status, next_at = "disabled", None
            else:
                status, next_at = "retry", now + min(3600, 30 * (2 ** (attempt - 1)))
            self.store._append(path, {"delivery_id": delivery_id,
                                      "endpoint_id": row.get("endpoint_id"),
                                      "event": row.get("event"), "status": status,
                                      "attempt": attempt, "next_attempt_at": next_at,
                                      "updated_at": stamp})
        return delivered

    def serve(self) -> None:
        while not self.stop.wait(5):
            self.run_once()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve, name="risk-webhooks", daemon=True)
        thread.start()
        return thread
