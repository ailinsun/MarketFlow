#!/usr/bin/env python3
"""Money-path heartbeat watchdog — it answers one question: is the execution
chain still writing?

Why it exists. Alert self-dependency comes in two shapes, and fixing one does not
fix the other:

  Shape one: the transport dies, so the alert saying "the transport died" cannot
  be sent either.

  Shape two, which this module fixes: **the trigger condition itself depends on
  the monitored thing still being alive.** A degradation check that reads a
  daemon's latest status and de-duplicates on its `generated_at` returns early
  the moment the daemon stops writing at all — so it never alerts. And "the
  daemon froze" is exactly the death that most needs reporting.

The test here therefore has to be **simpler than whatever it watches**:

  * It looks only at modification time and never parses content. A latest.json
    written non-atomically can be truncated when a process is killed mid-write; a
    content-based test breaks along with it, while an mtime test does not.
  * It imports nothing it watches — not one line of the execution modules. A
    guard that shares a code path dies at that shared layer alongside its target.
  * Delivery goes through the operator alert sink, which shares no code with the
    execution chain.

**Boundary, stated honestly.** This module can detect "stopped writing". It cannot
detect "still writing but wrong" — that needs a content test, which would
reintroduce the self-dependency. Content-level degradation stays with the
degradation check. The two are complementary and do not overlap.

**Who watches this one.** It writes its own heartbeat into latest.json, so
whatever monitors heartbeats across hosts sees it too. That cross-host hop also
covers "the whole machine went away".

Run:       python3 marketflow/risk/money_path.py [--dry] [--selftest]
As a service: every couple of minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
OUT_DIR = runtime_path("risk", "money_path")
LATEST_PATH = os.path.join(OUT_DIR, "latest.json")
HISTORY_PATH = os.path.join(OUT_DIR, "history.jsonl")

# (label, absolute path, max silence in seconds, plain-language description).
# Thresholds are several times each writer's own period: a false alarm is cheaper
# than a missed one, but alerting at exactly the write period guarantees noise.
TARGETS: list[tuple[str, str, int, str]] = [
    (
        "execution-daemon",
        # The daemon rewrites latest.json every tick. This path must equal the
        # daemon's DEFAULT_LATEST_JSON; tests/test_runtime_layout.py asserts it.
        runtime_path("execution", "daemon", "latest.json"),
        360,
        "the execution chain: exit evaluation, entry gating and HALT recovery all\n"
        "         happen inside this tick",
    ),
]

# Debounce: alert only after N consecutive stale cycles. A single-cycle blip is
# common (the instant of a write, a busy disk, a snapshot copy), while two cycles
# still guarantee an alert within minutes, which is fast enough for a chain with
# open positions.
STALE_CONFIRM_ROUNDS = 2

# ── Authorisation-expiry observation ────────────────────────────────────────
# An arm-state written with `expires_at` downgrades to exit_only on expiry rather
# than stopping outright. What this module owns is that **the downgrade must not
# happen silently**: one warning a week ahead, one two days ahead, one on the day.
#
# There is also a **legacy shape** to handle: an arm-state written without
# expires_at validates as "not expired", so it never stops by itself. Those get
# age-based reminders instead, and the text says how to re-arm with a
# reconfirmation interval attached.
ARM_STATE_FILE = runtime_path("execution", "polymarket_arm_state.json")
ARM_REVIEW_DAYS = (30, 60, 90, 180)   # age marks for the legacy shape; one reminder each
# With expires_at, whichever band the remaining days fall into is the one reported.
# **The downgrade must not be silent**: a week to decide on renewal, a final
# reminder two days out, and a clear statement of the state on the day itself.
ARM_EXPIRY_WARN_DAYS = ((0.0, "expired"), (2.0, "2d"), (7.0, "7d"))

# ── Authorisation-change observation ────────────────────────────────────────
# On a single-operator machine any local process is equivalent to the operator, so
# authenticating the arm endpoint does not stop the real threat: a process can read
# the token file. **What is achievable is making arming impossible to do
# silently.** Every arm-state write leaves a from -> to record, and this module
# reports each new one, including the operator's own, as a receipt.
#
# One class deserves emphasis: enabling deletes the global HALT, and most HALT
# flavours are the manual-forever kind —
#   * an epoch drawdown fuse tripped
#   * a credential leak guard tripped
#   * a ledger append failed after a live order (a ghost position)
# Only the reservation kind clears itself.
ARM_CHANGE_LOG = runtime_path("execution", "arm_change_log.jsonl")
HALT_AUTOCLEARABLE_MARKER = "reserve cap would be breached"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_write_json(path: str, data) -> None:
    """Atomic write. This module's output is somebody else's test, so it must never
    leave a half-written file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def check_targets(*, now: float | None = None) -> list[dict]:
    now_ts = now if now is not None else time.time()
    out = []
    for label, path, max_age, human in TARGETS:
        try:
            age = now_ts - os.path.getmtime(path)
        except OSError:
            age = None  # missing file = never written or deleted; counts as stale
        out.append({
            "label": label,
            "path": path,
            "age_sec": None if age is None else round(age),
            "max_age_sec": max_age,
            "stale": True if age is None else age > max_age,
            "human": human,
        })
    return out


def _alert_text(r: dict, *, recovered: bool) -> str:
    if recovered:
        return f"✅ Money-path heartbeat recovered — {r['label']} is writing again."
    age = ("never written / file absent" if r["age_sec"] is None
           else f"{r['age_sec']}s since the last write")
    return (
        f"🔴 Money-path heartbeat stopped — {r['label']} ({age}, threshold {r['max_age_sec']}s).\n"
        f"This chain covers: {r['human']}\n"
        "⚠️ If anything is open, exit evaluation is not running right now.\n"
        "Check that the execution daemon is alive, then read its log."
    )


def arm_age_observation(*, now: float | None = None) -> dict:
    """Read arm-state and work out how long this authorisation has been open.
    Read-only, fails soft, imports nothing from the execution layer.

    With armed=False the caller does nothing: a closed authorisation has no age
    problem.
    """
    now_ts = now if now is not None else time.time()
    out = {"present": False, "armed": False, "age_days": None, "milestone": None,
           "key": None, "mode": None, "has_expires_at": None, "days_left": None}
    doc = _read_json(ARM_STATE_FILE, None)
    if not isinstance(doc, dict):
        return out
    out["present"] = True
    out["armed"] = bool(doc.get("armed"))
    out["mode"] = str(doc.get("mode") or "")
    out["has_expires_at"] = bool(str(doc.get("expires_at") or "").strip())
    if not out["armed"]:
        return out
    stamp = str(doc.get("armed_at") or doc.get("updated_at") or "").strip()
    try:
        armed_at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return out
    age_days = (now_ts - armed_at) / 86400.0
    out["age_days"] = round(age_days, 1)

    # With expires_at: warn ahead, then notify on the day. Expiry itself is
    # handled downstream (a downgrade to exit_only, not a full stop), but **the
    # downgrade must not be silent** — there has to be a week to decide on renewal
    # rather than discovering one day that entry stopped.
    if out["has_expires_at"]:
        exp_stamp = str(doc.get("expires_at") or "").strip()
        try:
            exp_ts = datetime.fromisoformat(exp_stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return out
        days_left = (exp_ts - now_ts) / 86400.0
        out["days_left"] = round(days_left, 1)
        stage = None
        for threshold, name in ARM_EXPIRY_WARN_DAYS:
            if days_left <= threshold:
                stage = name
                break
        if stage:
            out["milestone"] = stage
            out["key"] = f"arm-expiry-{stage}@{exp_stamp}"
        return out

    # The legacy shape without expires_at never stops by itself, so remind on age.
    crossed = [d for d in ARM_REVIEW_DAYS if age_days >= d]
    if crossed:
        out["milestone"] = max(crossed)
        # the key carries armed_at, so re-arming starts a new authorisation and
        # resets the clock
        out["key"] = f"arm-age-{out['milestone']}d@{stamp}"
    return out


def _arm_age_text(obs: dict) -> str:
    m = obs["milestone"]
    if m == "expired":
        return (
            "🟠 Authorisation has expired — opening new positions has stopped, exits\n"
            "still run.\n"
            f"Now: mode downgraded to exit_only (the file still says {obs['mode']}) · open for {obs['age_days']} days\n"
            "Why not a full stop: stopping sells would turn open positions into\n"
            "unmanaged ones, which is more dangerous, not more conservative.\n"
            "To resume opening: enable it again; the clock restarts from that moment."
        )
    if m in ("7d", "2d"):
        return (
            f"🟡 Authorisation expires in {obs['days_left']} days "
            f"({'final reminder' if m == '2d' else 'advance notice'}).\n"
            f"Now: armed=true · mode={obs['mode']} · open for {obs['age_days']} days\n"
            "On expiry **only new positions stop; exits continue**. Doing nothing\n"
            "leaves no position unmanaged.\n"
            "To keep opening automatically: enable it again to renew."
        )
    return (
        f"🟠 Authorisation has been open for {m} days — worth reviewing.\n"
        f"Now: armed=true · mode={obs['mode']} · open for {obs['age_days']} days · **no expiry**\n"
        "This is the legacy shape with no expires_at, so it will never stop by\n"
        "itself.\n"
        "Re-enabling it attaches a reconfirmation interval; on expiry only new\n"
        "positions stop and exits continue."
    )


def arm_change_observation() -> dict:
    """Read the authorisation-change journal and return the last entry.
    Read-only, fails soft."""
    out = {"present": False, "last": None, "key": None, "manual_only": False,
           "caps_raised": False}
    try:
        with open(ARM_CHANGE_LOG, encoding="utf-8") as f:
            rows = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return out
    if not rows:
        return out
    out["present"] = True
    try:
        last = json.loads(rows[-1])
    except json.JSONDecodeError:
        return out
    if not isinstance(last, dict):
        return out
    out["last"] = last
    bodies = [str((c or {}).get("content") or "") for c in (last.get("cleared") or [])]
    # If any one of them is not the reservation kind, this is the sort that should
    # have been investigated before being cleared.
    out["manual_only"] = any(
        b and HALT_AUTOCLEARABLE_MARKER not in b for b in bodies)
    frm, to = last.get("from") or {}, last.get("to") or {}
    for field in ("max_total_deploy_usd", "max_per_trade_usd", "max_drawdown_usd"):
        a, b = frm.get(field), to.get(field)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a:
            out["caps_raised"] = True
    out["key"] = f"arm-change@{last.get('ts')}"
    return out


def _arm_change_text(obs: dict) -> str:
    """Receipt for an authorisation change. The operator receives one for their own
    action too. That is not noise, it is the evidence that only they can act:
    receiving one nobody triggered means something is wrong."""
    last = obs["last"] or {}
    frm, to = last.get("from") or {}, last.get("to") or {}
    action = last.get("action")
    head = {"enable": "🟢 Automated trading enabled",
            "disable": "⚪️ Automated trading disabled",
            "kill": "🛑 KILL engaged (every stop file written)"}.get(
                action, f"Authorisation change: {action}")
    if obs.get("manual_only"):
        head = ("🔴 " + head.lstrip("🟢⚪️🛑 ")
                + " — and it cleared a HALT that should have been investigated first")
    elif obs.get("caps_raised"):
        head = "🟠 " + head.lstrip("🟢⚪️🛑 ") + " — and the caps were raised"

    def _fmt(d):
        if not d.get("armed"):
            return "off"
        return (f"{d.get('mode')} · total ${d.get('max_total_deploy_usd')} / "
                f"per-trade ${d.get('max_per_trade_usd')} / drawdown ${d.get('max_drawdown_usd')}"
                + (f" · expires {str(d.get('expires_at'))[:10]}" if d.get("expires_at")
                   else " · no expiry"))

    lines = [head, f"Time: {last.get('ts')}",
             f"Change: {_fmt(frm)}  ->  {_fmt(to)}",
             f"Source: {'via tunnel' if last.get('via_tunnel_token') else 'local call'}"]
    for c in (last.get("cleared") or []):
        lines.append(f"Cleared: {(c or {}).get('file')} — "
                     f"{((c or {}).get('content') or '(empty)')[:160]}")
    if obs.get("manual_only"):
        lines.append("⚠️ A drawdown fuse, a credential leak guard and a failed ledger "
                     "append are all manual-forever HALTs: the cause should have been "
                     "established before clearing one. If this was not deliberate, "
                     "investigate now.")
    lines.append("**Did you not do this?** Then another process is writing arm-state. "
                 "Kill it and investigate.")
    return "\n".join(lines)


def _send(text: str) -> bool:
    """Deliver an operator alert. Even an import failure leaves a trace: a broken
    alerting chain must never be silent.

    The destination is chosen by `marketflow/monitor/notify.py` from the environment. With
    nothing configured this returns False and writes to stderr — a watchdog would
    rather be noisy than pretend it delivered."""
    try:
        from marketflow.monitor.notify import send_alert, configured
    except Exception as exc:  # noqa: BLE001
        print(f"[money-path-watchdog] marketflow.monitor.notify unavailable, cannot deliver: {exc!r}",
              file=sys.stderr, flush=True)
        return False
    if not configured():
        print("[money-path-watchdog] no alert destination configured "
              "(MARKETFLOW_ALERT_WEBHOOK / MARKETFLOW_ALERT_BOT_TOKEN+CHAT_ID); "
              "- nobody will receive the following:\n" + text, file=sys.stderr, flush=True)
        return False
    ok = send_alert(text)
    if not ok:
        print("[money-path-watchdog] alert not delivered (every configured destination failed)",
              file=sys.stderr, flush=True)
    return ok


def run_once(*, dry: bool = False, now: float | None = None) -> dict:
    results = check_targets(now=now)
    prev = _read_json(LATEST_PATH, {}) or {}
    prev_streaks = (prev.get("streaks") or {}) if isinstance(prev.get("streaks"), dict) else {}
    prev_notified = set(prev.get("notified") or [])

    streaks: dict[str, int] = {}
    notified: set[str] = set(prev_notified)
    messages: list[str] = []

    for r in results:
        label = r["label"]
        if r["stale"]:
            streaks[label] = int(prev_streaks.get(label) or 0) + 1
            r["stale_streak"] = streaks[label]
            if streaks[label] >= STALE_CONFIRM_ROUNDS and label not in notified:
                messages.append(("stale", label, _alert_text(r, recovered=False)))
        else:
            streaks[label] = 0
            r["stale_streak"] = 0
            if label in notified:
                messages.append(("recovered", label, _alert_text(r, recovered=True)))

    arm_obs = arm_age_observation(now=now)
    if arm_obs.get("key") and arm_obs["key"] not in notified:
        messages.append(("arm_age", arm_obs["key"], _arm_age_text(arm_obs)))

    change_obs = arm_change_observation()
    if change_obs.get("key") and change_obs["key"] not in notified:
        messages.append(("arm_change", change_obs["key"], _arm_change_text(change_obs)))

    sent_any = False
    for kind, label, text in messages:
        if dry:
            print(text)
            continue
        ok = _send(text)
        sent_any = sent_any or ok
        # Latch on the **send result**. Latching on the attempt would mean a failed
        # send is never retried for this outage, turning "chain dead -> alert lost ->
        # silence" into exactly the shape this module exists to fix.
        if kind in ("stale", "arm_age", "arm_change"):
            if ok:
                notified.add(label)
        else:
            if ok:
                notified.discard(label)

    record = {
        "schema_version": "money-path-watchdog-v1",
        "generated_at": iso_now(),
        "results": results,
        "streaks": streaks,
        "notified": sorted(notified),
        "arm_observation": arm_obs,
        "arm_change_observation": {k: v for k, v in change_obs.items() if k != "last"},
        "alerted_this_round": [label for _k, label, _t in messages],
        "sent": sent_any,
        "dry": dry,
    }
    if not dry:
        _atomic_write_json(LATEST_PATH, record)
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def selftest() -> int:
    checks: dict[str, bool] = {}
    now = 1_000_000.0

    fresh = os.path.join(OUT_DIR, ".selftest_fresh")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(fresh, "w", encoding="utf-8") as f:
        f.write("x")
    os.utime(fresh, (now - 10, now - 10))
    saved = list(TARGETS)
    try:
        TARGETS[:] = [("t", fresh, 360, "h")]
        checks["fresh_not_stale"] = check_targets(now=now)[0]["stale"] is False
        os.utime(fresh, (now - 400, now - 400))
        checks["old_is_stale"] = check_targets(now=now)[0]["stale"] is True
        checks["age_reported"] = check_targets(now=now)[0]["age_sec"] == 400

        TARGETS[:] = [("t", os.path.join(OUT_DIR, ".nope"), 360, "h")]
        missing = check_targets(now=now)[0]
        checks["missing_file_is_stale"] = missing["stale"] is True
        checks["missing_file_age_none"] = missing["age_sec"] is None
    finally:
        TARGETS[:] = saved
        try:
            os.remove(fresh)
        except OSError:
            pass

    # The test never parses content: a truncated JSON must still be judged fresh,
    # because non-atomic writes are a fact of life.
    broken = os.path.join(OUT_DIR, ".selftest_broken")
    with open(broken, "w", encoding="utf-8") as f:
        f.write('{"half":')
    os.utime(broken, (now - 5, now - 5))
    try:
        TARGETS[:] = [("t", broken, 360, "h")]
        checks["truncated_json_still_judged_fresh"] = check_targets(now=now)[0]["stale"] is False
    finally:
        TARGETS[:] = saved
        try:
            os.remove(broken)
        except OSError:
            pass

    # Zero shared code path: a watchdog that imports what it watches dies with it.
    # Checked on the syntax tree, so it tracks the real import paths rather than a
    # list of names that can go stale when modules move.
    import ast as _ast
    tree = _ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    checks["no_import_of_monitored_code"] = not any(
        m == "marketflow.execution" or m.startswith("marketflow.execution.")
        for m in imported)

    # the shape of the real target table: breaking it must be visible immediately
    checks["real_target_is_the_daemon_latest"] = (
        len(saved) >= 1 and saved[0][0] == "execution-daemon"
        and saved[0][1].endswith("execution/daemon/latest.json")
    )
    checks["real_threshold_is_six_slow_ticks"] = saved[0][2] == 360
    checks["debounce_at_least_two_rounds"] = STALE_CONFIRM_ROUNDS >= 2

    # authorisation-age observation
    import tempfile
    saved_arm = ARM_STATE_FILE
    d = tempfile.mkdtemp(prefix="mpw-arm-")
    try:
        globals()["ARM_STATE_FILE"] = os.path.join(d, "arm.json")
        t0 = 1_700_000_000.0
        def _write(doc):
            with open(ARM_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(doc, f)
        def iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")

        _write({"armed": False, "mode": "off"})
        checks["arm_disarmed_no_milestone"] = arm_age_observation(now=t0)["milestone"] is None

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 20 * 86400)})
        o = arm_age_observation(now=t0)
        checks["arm_20d_no_milestone"] = o["milestone"] is None and o["age_days"] == 20.0

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 31 * 86400)})
        o = arm_age_observation(now=t0)
        checks["arm_31d_hits_30d_milestone"] = o["milestone"] == 30
        checks["arm_key_binds_armed_at"] = o["key"].endswith(iso(t0 - 31 * 86400))

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 200 * 86400)})
        checks["arm_200d_takes_highest"] = arm_age_observation(now=t0)["milestone"] == 180

        # An arm-state with expires_at is handled by the execution layer's own
        # expiry test; this module warns by remaining-days band rather than
        # duplicating it.
        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 20 * 86400),
                "expires_at": iso(t0 + 10 * 86400)})
        o = arm_age_observation(now=t0)
        checks["expiry_far_away_no_warning"] = o["milestone"] is None and o["days_left"] == 10.0

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 25 * 86400),
                "expires_at": iso(t0 + 5 * 86400)})
        checks["expiry_7d_warns"] = arm_age_observation(now=t0)["milestone"] == "7d"

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 29 * 86400),
                "expires_at": iso(t0 + 86400)})
        checks["expiry_2d_warns_last_call"] = arm_age_observation(now=t0)["milestone"] == "2d"

        _write({"armed": True, "mode": "full", "armed_at": iso(t0 - 31 * 86400),
                "expires_at": iso(t0 - 86400)})
        o = arm_age_observation(now=t0)
        checks["expiry_past_reports_expired"] = o["milestone"] == "expired"
        checks["expiry_key_binds_expiry_stamp"] = o["key"].endswith(iso(t0 - 86400))
        # every band's text must render, so a KeyError is not discovered on the day
        # it matters
        checks["expiry_texts_render"] = all(
            len(_arm_age_text({**o, "milestone": m, "days_left": 1.0})) > 40
            for m in ("expired", "7d", "2d", 30))

        # a corrupt file must not crash the watchdog: it is somebody else's test
        with open(ARM_STATE_FILE, "w", encoding="utf-8") as f:
            f.write('{"half":')
        checks["arm_broken_json_fail_soft"] = arm_age_observation(now=t0)["present"] is False
        os.remove(ARM_STATE_FILE)
        checks["arm_missing_fail_soft"] = arm_age_observation(now=t0)["present"] is False
    finally:
        globals()["ARM_STATE_FILE"] = saved_arm

    # HALT-clearing observation
    saved_halt = ARM_CHANGE_LOG
    d2 = tempfile.mkdtemp(prefix="mpw-halt-")
    try:
        globals()["ARM_CHANGE_LOG"] = os.path.join(d2, "arm_change_log.jsonl")
        checks["armchg_no_log_no_alert"] = arm_change_observation()["key"] is None

        def _append(row):
            with open(ARM_CHANGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

        _append({"ts": "2026-01-01T00:00:00Z", "action": "enable", "mode": "full",
                 "cleared": [{"file": "HALT", "content":
                              "2026-01-01 at-risk plus pending " + HALT_AUTOCLEARABLE_MARKER}]})
        o = arm_change_observation()
        checks["armchg_reserve_flavor_not_manual"] = o["manual_only"] is False and o["key"] is not None

        _append({"ts": "2026-01-02T00:00:00Z", "action": "enable", "mode": "full",
                 "cleared": [{"file": "HALT", "content":
                              "2026-01-02 epoch drawdown fuse tripped (realized loss 100 >= 100)"}]})
        o = arm_change_observation()
        checks["armchg_drawdown_flavor_is_manual"] = o["manual_only"] is True
        checks["armchg_key_tracks_latest"] = o["key"].endswith("2026-01-02T00:00:00Z")

        _append({"ts": "2026-01-03T00:00:00Z", "action": "enable", "mode": "full",
                 "cleared": [{"file": "HALT", "content": "leak guard tripped after live order"}]})
        checks["armchg_leak_flavor_is_manual"] = arm_change_observation()["manual_only"] is True

        # raised caps must be visible, which needs a from -> to comparison rather
        # than a look at the current value
        _append({"ts": "2026-01-04T00:00:00Z", "action": "enable",
                 "from": {"armed": True, "mode": "full", "max_total_deploy_usd": 200.0},
                 "to": {"armed": True, "mode": "full", "max_total_deploy_usd": 2000.0},
                 "cleared": []})
        o = arm_change_observation()
        checks["armchg_detects_caps_raised"] = o["caps_raised"] is True
        checks["armchg_plain_change_not_manual"] = o["manual_only"] is False

        _append({"ts": "2026-01-05T00:00:00Z", "action": "disable",
                 "from": {"armed": True, "mode": "full", "max_total_deploy_usd": 2000.0},
                 "to": {"armed": False, "mode": "off", "max_total_deploy_usd": 2000.0},
                 "cleared": []})
        o = arm_change_observation()
        checks["armchg_caps_unchanged_not_flagged"] = o["caps_raised"] is False
        # every action's text must render, so a KeyError is not discovered on the
        # day it matters
        checks["armchg_texts_render"] = all(
            len(_arm_change_text({**o, "manual_only": m, "caps_raised": c})) > 60
            for m in (True, False) for c in (True, False))

        with open(ARM_CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write('{"broken":\n')
        checks["armchg_broken_row_fail_soft"] = arm_change_observation()["key"] is None
    finally:
        globals()["ARM_CHANGE_LOG"] = saved_halt

    ok = all(checks.values())
    print(json.dumps({"PASS": ok, "n": len(checks), "checks": checks},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry", action="store_true",
                   help="print alerts only; send nothing and write nothing")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)
    if args.selftest:
        return selftest()
    rec = run_once(dry=args.dry)
    print(json.dumps({"generated_at": rec["generated_at"],
                      "results": [{k: v for k, v in r.items() if k != "human"} for r in rec["results"]],
                      "alerted": rec["alerted_this_round"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
