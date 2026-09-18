"""Heartbeat watchdog: monitors critical processes by file freshness, alerts once
when one goes silent past its threshold, and alerts once again when it recovers.

It covers the blind spot that any content-based health check has. A check that
inspects what a process wrote stays quiet when the process stops writing
altogether — the failure it most needs to catch. This module looks only at
modification time and shares no code path with anything it watches, because the
thing that watches the watchdog has to be independent of it.

Thresholds are set at several times each process's own write period. A false
alarm is cheaper than a missed one, but alerting at exactly the write period
guarantees noise on the first slow cycle.

A state file suppresses repeat notifications, so run this on a short timer
without worrying about duplicates.

**Cross-process and cross-machine heartbeats belong in this table too.** A
watchdog whose target list contains only itself cannot see the two failures that
matter most: the execution chain stopping, and the machine it runs on going away.
When a heartbeat is mirrored from another host, preserve the source mtime during
the copy — one mtime test then covers all three deaths at once (process frozen,
mirror stopped, host gone) — and set the threshold from the mirroring period
rather than the watched process's own tick, since the mirror sets the ceiling on
how fresh anything can look from here.

CLI: python -m marketflow.monitor.watchdog [--dry] [--selftest]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
from datetime import datetime, timezone  # noqa: E402

from marketflow.paths import runtime_dir, runtime_path  # noqa: E402

# This package's own bucket: the watchdog's de-duplication state lives here, apart
# from every heartbeat it reads.
OUT_DIR = runtime_path("monitor")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


# (label, path, max silence in seconds, plain-language description). A path under
# EXEC_PREFIX resolves against the runtime tree -- that bucket belongs to the
# execution stack, possibly mirrored from another host. Everything else resolves
# against OUT_DIR, this package's own bucket. Two buckets, two writers, never
# mixed. The chain is: the execution daemon writes a per-tick heartbeat, the
# money-path watchdog watches it, and this module watches the money-path watchdog.
EXEC_PREFIX = ("risk/", "execution/", "guardian/", "feeds/")
TARGETS: list[tuple[str, str, int, str]] = [
    ("money-path-watchdog",
     "risk/money_path/latest.json",
     900,
     "the money-path watchdog's own heartbeat (the watchdog's watchdog)"),
]


def _resolve(rel: str) -> str:
    """Mirrored heartbeats resolve against PROJECT_DIR; this package's own
    artefacts resolve against OUT_DIR."""
    base = runtime_dir() if rel.startswith(EXEC_PREFIX) else OUT_DIR
    return os.path.join(base, rel)


def _state_path() -> str:
    return os.path.join(OUT_DIR, "watchdog_state.json")


def check_targets(*, now: float | None = None) -> list[dict[str, Any]]:
    now_ts = now if now is not None else time.time()
    out = []
    for label, rel, max_age, human in TARGETS:
        path = _resolve(rel)
        try:
            age = now_ts - os.path.getmtime(path)
        except OSError:
            age = float("inf")
        out.append({"label": label, "path": path, "age_sec": None if age == float("inf") else round(age),
                    "max_age_sec": max_age, "stale": age > max_age, "human": human})
    return out


def run_once(*, dry: bool = False) -> dict[str, Any]:
    results = check_targets()
    prev = _read_json(_state_path(), {})
    prev_stale = set(prev.get("stale_labels") or [])
    now_stale = {r["label"] for r in results if r["stale"]}
    newly_stale = now_stale - prev_stale
    recovered = prev_stale - now_stale

    msgs: list[str] = []
    for r in results:
        if r["label"] in newly_stale:
            age = "never written" if r["age_sec"] is None else f"{r['age_sec']}s since last update"
            msgs.append(f"🔴 watchdog: {r['label']} heartbeat timed out "
                        f"({age}, threshold {r['max_age_sec']}s)\n"
                        f"This one covers: {r['human']}\n"
                        "⚠️ Money path: if anything is open, exit evaluation may have "
                        "stopped. Check the execution daemon and whatever writes this "
                        "heartbeat.")
    for label in recovered:
        msgs.append(f"🟢 watchdog: {label} heartbeat recovered")

    if msgs and not dry:
        # Heartbeat alerts go to the operator sink only, never to a user-facing
        # channel.
        try:
            from marketflow.monitor.notify import send_alert
            if not all(send_alert(m) for m in msgs):
                return {"results": results, "notified": False, "messages": msgs}
        except Exception:  # noqa: BLE001 - a failed send must not be recorded as sent
            return {"results": results, "notified": False, "messages": msgs}
    if not dry:
        _atomic_write_json(_state_path(), {"stale_labels": sorted(now_stale),
                                           "checked_at": _iso_now()})
    return {"results": results, "notified": bool(msgs), "messages": msgs}


def selftest() -> int:
    """Offline invariants. Sends nothing and writes no state: it checks the target
    table and the path-resolution rules only."""
    checks: dict[str, bool] = {}
    labels = {t[0] for t in TARGETS}

    # The point of the module: the money path must be in the table, or this is
    # back to watching only itself.
    checks["covers_money_path"] = "money-path-watchdog" in labels
    # Every target is another process's heartbeat; a table that watches only this
    # module's own files would be watching itself.
    checks["every_target_is_cross_process"] = bool(TARGETS) and all(
        t[1].startswith(EXEC_PREFIX) for t in TARGETS)

    # The two buckets must not resolve to the same base. Mixing them means a
    # mirrored heartbeat is never actually read, and nothing reports an error.
    checks["cross_process_resolves_under_runtime"] = _resolve(
        "risk/money_path/latest.json") == os.path.join(
        runtime_dir(), "risk", "money_path", "latest.json")
    checks["own_state_resolves_under_out_dir"] = _resolve("watchdog_state.json") == os.path.join(
        OUT_DIR, "watchdog_state.json")
    checks["two_buckets_differ"] = os.path.dirname(_resolve("watchdog_state.json")) != os.path.dirname(
        _resolve("execution/x.json"))

    # Cross-process thresholds follow the writing period, not the watched
    # process's own tick: one missed write should not alert, two in a row should.
    checks["cross_process_threshold_tolerates_two_misses"] = all(t[2] >= 900 for t in TARGETS)

    # A missing file counts as stale. A heartbeat that never wrote successfully
    # must alert, not be silently read as "not configured".
    saved = list(TARGETS)
    try:
        TARGETS[:] = [("t", "trade/__nope__/none.json", 900, "h")]
        r = check_targets()[0]
        checks["missing_heartbeat_is_stale"] = r["stale"] is True and r["age_sec"] is None
    finally:
        TARGETS[:] = saved

    checks["every_target_has_human_text"] = all(len(t) == 4 and t[3] for t in TARGETS)

    ok = all(checks.values())
    print(json.dumps({"PASS": ok, "n": len(checks), "checks": checks},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="heartbeat watchdog")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    print(json.dumps(run_once(dry=args.dry), ensure_ascii=False, indent=2))
