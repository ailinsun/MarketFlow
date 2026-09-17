"""Append-only latency ledger: runtime/alerts/sla_ledger.jsonl.

One row per delivered alert, three timestamps compressed into two latencies:

  e2e_ms  = sent - cycle_start   (this monitoring cycle started pulling data ->
                                  the delivery API acknowledged the send)
  disp_ms = sent - fire          (the rule fired -> the delivery API acknowledged)

Any latency commitment should be stated on the e2e_ms basis. The delay between
something happening on-chain and it becoming visible in an upstream API is not
measurable from here, so a promise that implied it would be unfounded.

A sidecar write: it never changes alert logic, and it fails soft.

CLI: python -m sla --days 7   -> P50/P95/max/over-budget summary as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.monitor import store as S  # noqa: E402

DEFAULT_SLA_MS = 60_000


def _path() -> str:
    return os.path.join(S.OUT_DIR, "sla_ledger.jsonl")


def record_alert(chat_id: Any, alert: dict[str, Any], *, cycle_ts: float,
                 fire_ts: float, sent_ts: float | None = None) -> bool:
    """Record one delivered alert. Returns False on a failed write."""
    sent = sent_ts if sent_ts is not None else time.time()
    row = {"t": round(sent, 3), "chat_id": str(chat_id),
           "rule": str(alert.get("rule") or "?"), "scope": str(alert.get("scope") or "?"),
           "e2e_ms": max(0, int((sent - cycle_ts) * 1000)),
           "disp_ms": max(0, int((sent - fire_ts) * 1000))}
    try:
        os.makedirs(S.OUT_DIR, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except OSError:
        return False


def _pct(sorted_ms: list[int], q: float) -> int:
    if not sorted_ms:
        return 0
    idx = min(len(sorted_ms) - 1, max(0, int(round(q * (len(sorted_ms) - 1)))))
    return sorted_ms[idx]


def summarize(*, since_ts: float | None = None, sla_ms: int = DEFAULT_SLA_MS) -> dict[str, Any]:
    """Ledger summary: n, P50, P95, max, count over budget (e2e_ms > sla_ms),
    and per-rule counts."""
    rows: list[dict[str, Any]] = []
    try:
        with open(_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_ts is None or float(row.get("t") or 0.0) >= since_ts:
                    rows.append(row)
    except OSError:
        pass
    e2e = sorted(int(r.get("e2e_ms") or 0) for r in rows)
    by_rule: dict[str, int] = {}
    for r in rows:
        by_rule[str(r.get("rule") or "?")] = by_rule.get(str(r.get("rule") or "?"), 0) + 1
    return {"n": len(e2e), "sla_ms": sla_ms,
            "p50_ms": _pct(e2e, 0.50), "p95_ms": _pct(e2e, 0.95),
            "max_ms": e2e[-1] if e2e else 0,
            "breaches": sum(1 for v in e2e if v > sla_ms),
            "by_rule": by_rule}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SLA ledger summary")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--sla-ms", type=int, default=DEFAULT_SLA_MS)
    args = ap.parse_args()
    print(json.dumps(summarize(since_ts=time.time() - args.days * 86400,
                               sla_ms=args.sla_ms), ensure_ascii=False, indent=2))
