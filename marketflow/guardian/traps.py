"""Guardian structural-trap interception — three rules, none of which needs a
prediction to be right.

Where hosted users actually lose money, measured first-hand over 1.94M fills and
724 complete address histories:

  cheap_ticket   BUY under $0.10. That band is 1.36% of all deployed capital and
                 lost more money than the entire sample's net loss; with it
                 removed the remaining 98.6% is break-even after fees (+0.107%).
                 An accounting identity over the window, not a statistical claim.
                 A flat gate, deliberately: the damage sits in the 2-5c band and
                 px<0.02 shows no bias, so "cheaper is worse" would be false.
  night_lottery  BUY under $0.20 inside the user's OWN local 18:00-02:00. Ships
                 in shadow and should stay there: seven tenths of what this cell
                 loses is the sub-dime part cheap_ticket already takes, and the
                 remainder (0.10-0.20) shows a 24pp day/night gap that collapses
                 to 0.76pp once the five largest wallets are removed, with a
                 within-wallet paired median of -0.04pp. The rule is built and
                 tested; the evidence for enforcing it is not there.
  zombie         Tickets the market has already priced at ~0 and the holder never
                 touches again: 17.95% of buy volume, on 65% of addresses.
                 Advisory only — it is told, never blocked.

The night rule is a claim about one price band inside a window. "Trading at night
is worse" on its own is NULL (within-wallet paired sign test p=0.9528, 158
wallets) and must never be asserted anywhere, in code or in copy. Above $0.20 the
night side of the same data is in fact BETTER.

Timezone is per user: what they declared, else their own silent window, else the
night rule does not apply to them. UTC is never assumed and a zone is never
guessed from anything else.

Interception is a guard rail, not custody of the decision: a user who explicitly
says "buy it anyway" with a reason gets their order, and the override is what
lands in the audit trail.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

from marketflow.guardian import store as gstore

# --- rule identity -----------------------------------------------------------
RULE_CHEAP_TICKET = "cheap_ticket"
RULE_NIGHT_LOTTERY = "night_lottery"
RULE_ZOMBIE = "zombie"

BUY_RULES = (RULE_CHEAP_TICKET, RULE_NIGHT_LOTTERY)

# Price gates. Both are hard edges of a measured band, not tunables.
CHEAP_PRICE_FLOOR = 0.10
NIGHT_PRICE_FLOOR = 0.20
# The night rule models an individual's own evening hours. A desk that trades
# around the clock, or across time zones, has no such window, so the rule is off
# unless a deployment turns it on for books where it means something.
NIGHT_RULE_ENABLED = os.environ.get("MARKETFLOW_NIGHT_RULE_ENABLED", "").strip().lower() in ("1", "true", "yes")
# Local clock hours [18:00, 02:00) — the user's evening through small hours.
NIGHT_START_LOCAL_HOUR = 18
NIGHT_END_LOCAL_HOUR = 2
# A ticket the book prices at or under 1 cent is what the analysis counted as
# permanently stuck capital.
ZOMBIE_PRICE_MAX = 0.01

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

# Shadow until deliberately promoted. Unlike the money gates (absent file = safe),
# the safe default here is the INERT one: enforcing puts a wall in front of a
# paying user's order, so switching it on is a product decision someone makes on
# purpose rather than a side effect of deploying this file.
DEFAULT_MODE = MODE_SHADOW

TRAPS_DIR = os.path.join(gstore.GUARDIAN_ROOT, "traps")
# Complete decision log INCLUDING shadow no-ops: the count of what a rule would
# have done is the whole point before it is promoted. store.audit stays the
# record of things that actually changed an outcome.
TRAPS_LOG = os.path.join(gstore.GUARDIAN_ROOT, "traps.jsonl")

# --- timezone inference ------------------------------------------------------
# Every person has one run of low-activity hours; its midpoint is about local
# 03:00. Anything that does not show one (bots, shared desks, too little history)
# is left undetermined rather than assigned a plausible zone.
TZ_QUIET_LEN_HOURS = 5
TZ_QUIET_RATIO_GATE = 0.5
MIN_TRADES_FOR_TZ = 30
# A fill this close to one of our own orders is one of our own orders.
OWN_FILL_MATCH_SEC = 300.0
TZ_PROFILE_TTL_SEC = 86400.0
TZ_PROFILE_FILE = "tz_profile.json"
TZ_SOURCE_DECLARED = "user_declared"
TZ_SOURCE_INFERRED = "inferred_silence"

DATA_API_BASE = "https://data-api.polymarket.com"
ACTIVITY_PAGE_LIMIT = 500
ACTIVITY_MAX_PAGES = 6
HTTP_TIMEOUT_SEC = 15.0


# --- pure rule core (no I/O; the shadow replay runs the same functions) -------

def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def check_cheap_ticket(price: float | None) -> dict[str, Any]:
    """C1. Unknown price does not trip it — a guard rail may not fire on a guess."""
    tripped = price is not None and 0 < float(price) < CHEAP_PRICE_FLOOR
    return {"rule": RULE_CHEAP_TICKET, "tripped": bool(tripped),
            "price": price, "floor": CHEAP_PRICE_FLOOR}


def in_night_window(local_hour: float | None) -> bool:
    if local_hour is None:
        return False
    h = float(local_hour) % 24.0
    return h >= NIGHT_START_LOCAL_HOUR or h < NIGHT_END_LOCAL_HOUR


def check_night_lottery(price: float | None, local_hour: float | None) -> dict[str, Any]:
    """C2. Needs the rule enabled for this deployment, plus BOTH a price and a
    known local hour. Without the hour the rule does not apply to this book, and
    UTC is never substituted for it."""
    night = in_night_window(local_hour)
    applicable = NIGHT_RULE_ENABLED and local_hour is not None
    tripped = (applicable and price is not None
               and night and 0 < float(price) < NIGHT_PRICE_FLOOR)
    return {"rule": RULE_NIGHT_LOTTERY, "tripped": bool(tripped),
            "price": price, "floor": NIGHT_PRICE_FLOOR,
            "local_hour": (round(float(local_hour), 2) if local_hour is not None else None),
            "in_night_window": bool(night),
            "enabled": NIGHT_RULE_ENABLED,
            "applicable": applicable}


def local_hour_at(ts_utc: float, tz_offset_hours: float | None) -> float | None:
    if tz_offset_hours is None:
        return None
    utc_hour = (float(ts_utc) % 86400.0) / 3600.0
    return (utc_hour + float(tz_offset_hours)) % 24.0


def infer_tz_offset(hour_hist: list[int]) -> tuple[int | None, float | None]:
    """24-bin UTC activity histogram -> whole-hour UTC offset, or None.

    Circular minimum over TZ_QUIET_LEN_HOURS; its midpoint is taken as local
    03:00. A quiet window that is not meaningfully quieter than a uniform day
    (ratio above the gate) means there is no sleep signal to read."""
    total = sum(hour_hist)
    if total <= 0 or len(hour_hist) != 24:
        return None, None
    best_sum, best_start = None, 0
    for start in range(24):
        s = sum(hour_hist[(start + i) % 24] for i in range(TZ_QUIET_LEN_HOURS))
        if best_sum is None or s < best_sum:
            best_sum, best_start = s, start
    expected = total * TZ_QUIET_LEN_HOURS / 24.0
    ratio = (best_sum / expected) if expected > 0 else None
    if ratio is None or ratio > TZ_QUIET_RATIO_GATE:
        return None, ratio
    mid = (best_start + (TZ_QUIET_LEN_HOURS - 1) / 2.0) % 24
    off = 3.0 - mid
    while off < -12:
        off += 24
    while off >= 12:
        off -= 24
    return int(round(off)), ratio


def zombie_report(positions: Iterable[dict[str, Any]], *,
                  price_max: float = ZOMBIE_PRICE_MAX) -> dict[str, Any]:
    """C3. What of this wallet's open book the market has already written off, plus
    the settled-but-unclaimed side, which is the half the user can still act on.

    Shares are of the CURRENTLY OPEN cost basis — deliberately not the 17.95%
    lifetime-buy-volume figure, which is a different denominator."""
    n = 0
    zombie_cost = 0.0
    open_cost = 0.0
    redeemable_usd = 0.0
    n_redeemable = 0
    items: list[dict[str, Any]] = []
    for pos in positions or []:
        shares = _to_float(pos.get("held_shares")) or 0.0
        if shares <= 0:
            continue
        entry_px = _to_float(pos.get("entry_price"))
        cost = (entry_px * shares) if entry_px is not None else None
        if cost is not None:
            open_cost += cost
        if pos.get("redeemable") is True:
            value = _to_float(pos.get("current_value_usd")) or 0.0
            if value > 0:
                n_redeemable += 1
                redeemable_usd += value
                continue
        px = _to_float(pos.get("current_sell_price"))
        if px is None or px > price_max:
            continue
        n += 1
        if cost is not None:
            zombie_cost += cost
        items.append({
            "market_slug": pos.get("market_slug"),
            "title": pos.get("title"),
            "outcome": pos.get("outcome"),
            "held_shares": round(shares, 4),
            "current_price": px,
            "cost_usd": round(cost, 2) if cost is not None else None,
        })
    items.sort(key=lambda r: (r["cost_usd"] or 0.0), reverse=True)
    return {
        "n_zombie": n,
        "zombie_cost_usd": round(zombie_cost, 2),
        "open_cost_usd": round(open_cost, 2),
        "zombie_share_of_open_cost": (round(zombie_cost / open_cost, 4) if open_cost > 0 else None),
        "n_redeemable": n_redeemable,
        "redeemable_value_usd": round(redeemable_usd, 2),
        # `items` is the display cut; `keys` covers every one of them, because the
        # already-told fingerprint has to span the whole set or a ticket beyond the
        # cut would be announced as new the day it happens to rank into view.
        "items": items[:20],
        "keys": [f"{it.get('market_slug')}|{it.get('outcome')}" for it in items],
    }


ZOMBIE_STATE_FILE = "zombie_seen.json"


def zombie_check(tenant_id: str, positions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """C3 with change detection, so the prompt fires when a ticket newly dies
    rather than every tick for the rest of the tenant's life. `new_items` is what
    a notification should be built from; the totals are for display."""
    rep = zombie_report(positions)
    rep["mode"] = rule_mode(RULE_ZOMBIE)
    path = os.path.join(gstore.tenant_dir(tenant_id), ZOMBIE_STATE_FILE)
    seen: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        seen = set(doc.get("seen") or []) if isinstance(doc, dict) else set()
    except (OSError, ValueError):
        seen = set()
    keys = rep["keys"]
    new = [k for k in keys if k not in seen]
    rep["new_items"] = new
    if rep["mode"] == MODE_OFF:
        return rep
    if new:
        log_trap({"event": "trap_zombie", "tenant_id": tenant_id, "mode": rep["mode"],
                  "n_zombie": rep["n_zombie"], "zombie_cost_usd": rep["zombie_cost_usd"],
                  "new_items": new, "n_redeemable": rep["n_redeemable"],
                  "redeemable_value_usd": rep["redeemable_value_usd"]})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seen": sorted(seen | set(keys)), "updated": gstore.iso_now()}, fh)
        os.replace(tmp, path)
    return rep


# --- rule mode (independent per-rule switch) ---------------------------------

def mode_file(rule: str) -> str:
    return os.path.join(TRAPS_DIR, f"{rule}.mode")


def rule_mode(rule: str) -> str:
    """Fleet mode for one rule. Unreadable or unrecognised content falls back to
    the default rather than to enforce — a corrupted file must not be able to
    start blocking orders."""
    try:
        with open(mode_file(rule), encoding="utf-8") as fh:
            val = fh.read().strip().lower()
        return val if val in MODES else DEFAULT_MODE
    except OSError:
        return DEFAULT_MODE


def set_rule_mode(rule: str, mode: str) -> str:
    if rule not in (RULE_CHEAP_TICKET, RULE_NIGHT_LOTTERY, RULE_ZOMBIE):
        raise ValueError(f"unknown trap rule {rule!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    os.makedirs(TRAPS_DIR, exist_ok=True)
    tmp = os.path.join(TRAPS_DIR, f".{rule}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(mode + "\n")
    os.replace(tmp, mode_file(rule))
    return mode


def log_trap(row: dict[str, Any]) -> None:
    gstore.append_jsonl(TRAPS_LOG, {"ts": gstore.iso_now(), **row})


# --- tenant timezone profile -------------------------------------------------

def _http_get_json(url: str, *, timeout: float = HTTP_TIMEOUT_SEC) -> Any:
    """Public read, explicitly direct. An env proxy here would route a plain
    public read through the trading tunnel, so a dead tunnel would silently take
    the timezone (and with it the night rule) down with it."""
    req = urllib.request.Request(url, headers={"User-Agent": "marketflow-guardian-traps"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_activity_rows(address: str, *,
                        max_pages: int = ACTIVITY_MAX_PAGES) -> tuple[list[dict[str, Any]], bool]:
    """This wallet's own fills from the public activity feed (read-only, no auth).

    `ok=False` marks a read that FAILED, as opposed to a wallet that has not
    traded. The two look identical in the numbers and must not be conflated:
    treating a dead feed as "no history" would erase a known timezone, and with
    it the night rule, every time the feed hiccups."""
    out: list[dict[str, Any]] = []
    addr = str(address or "").strip()
    if not addr:
        return out, True
    for page in range(max_pages):
        url = (f"{DATA_API_BASE}/activity?user={addr}"
               f"&limit={ACTIVITY_PAGE_LIMIT}&offset={page * ACTIVITY_PAGE_LIMIT}")
        try:
            rows = _http_get_json(url)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return out, False
        if not isinstance(rows, list) or not rows:
            break
        out.extend(r for r in rows
                   if isinstance(r, dict) and str(r.get("type") or "").upper() == "TRADE")
        if len(rows) < ACTIVITY_PAGE_LIMIT:
            break
    return out, True


def own_order_times(tenant_id: str) -> list[float]:
    """When Guardian itself placed an order for this tenant, from its execution
    ledger."""
    out: list[float] = []
    try:
        with open(os.path.join(gstore.tenant_dir(tenant_id), "ledger.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                stamp = str(row.get("generated_at") or "")
                try:
                    out.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def fetch_activity_hours(address: str, *, max_pages: int = ACTIVITY_MAX_PAGES,
                         exclude_times: Iterable[float] = ()) -> tuple[list[int], int, bool]:
    """UTC hour histogram of this wallet's fills -> (24 bins, n_trades, ok).

    `exclude_times` drops fills our own automation produced. Measured first-hand
    on a fully daemon-driven account: the silent window it reports is the
    automation's schedule, not the person's sleep (UTC-7 for an owner who lives
    at UTC+7). Reading a robot's rhythm and calling it a bedtime would put the
    night rule on the wrong eight hours of somebody's day."""
    rows, ok = fetch_activity_rows(address, max_pages=max_pages)
    hist, n = histogram_from_rows(rows, exclude_times=exclude_times)
    return hist, n, (ok or n >= MIN_TRADES_FOR_TZ)


def histogram_from_rows(rows: Iterable[dict[str, Any]], *,
                        exclude_times: Iterable[float] = ()) -> tuple[list[int], int]:
    ours = [float(t) for t in exclude_times]
    hist = [0] * 24
    n = 0
    for r in rows:
        ts = _to_float(r.get("timestamp"))
        if ts is None:
            continue
        if any(abs(ts - t) <= OWN_FILL_MATCH_SEC for t in ours):
            continue
        hist[int((ts % 86400) // 3600)] += 1
        n += 1
    return hist, n


def declared_offset(entry: dict[str, Any] | None) -> float | None:
    """A zone the user stated themselves. Ground truth beats inference, and it is
    not a guess — the alternative to reading it is telling a user we cannot apply
    a rule to the timezone they just gave us."""
    rules = entry.get("rules") if isinstance(entry, dict) and isinstance(entry.get("rules"), dict) else {}
    off = _to_float(rules.get("tz_offset_hours"))
    if off is None or not (-12.0 <= off <= 14.0):
        return None
    return off


def tz_profile_path(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(tenant_id), TZ_PROFILE_FILE)


def load_tz_profile(tenant_id: str) -> dict[str, Any] | None:
    try:
        with open(tz_profile_path(tenant_id), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def refresh_tz_profile(tenant_id: str, funder_address: str | None, *,
                       entry: dict[str, Any] | None = None,
                       now_ts: float | None = None,
                       hours_fetcher: Any = None) -> dict[str, Any]:
    """Recompute and persist this tenant's zone. Cheap and TTL-bounded: the
    histogram only moves as they trade."""
    now = float(now_ts if now_ts is not None else _now_ts())
    declared = declared_offset(entry)
    if declared is not None:
        prof = {"offset_hours": declared, "source": TZ_SOURCE_DECLARED,
                "n_trades": None, "quiet_ratio": None, "updated_ts": now}
        _write_tz_profile(tenant_id, prof)
        return prof
    cached = load_tz_profile(tenant_id)
    if cached and (now - float(cached.get("updated_ts") or 0)) < TZ_PROFILE_TTL_SEC:
        return cached
    fetch = hours_fetcher or fetch_activity_hours
    hist, n, ok = fetch(funder_address, exclude_times=own_order_times(tenant_id))
    if not ok:
        return cached or {"offset_hours": None, "source": TZ_SOURCE_INFERRED,
                          "n_trades": None, "quiet_ratio": None,
                          "reason": "activity_read_failed", "updated_ts": None}
    if n < MIN_TRADES_FOR_TZ:
        prof = {"offset_hours": None, "source": TZ_SOURCE_INFERRED, "n_trades": n,
                "quiet_ratio": None, "reason": "insufficient_history", "updated_ts": now}
        _write_tz_profile(tenant_id, prof)
        return prof
    off, ratio = infer_tz_offset(hist)
    prof = {"offset_hours": off, "source": TZ_SOURCE_INFERRED, "n_trades": n,
            "quiet_ratio": (round(ratio, 4) if ratio is not None else None),
            "updated_ts": now}
    if off is None:
        prof["reason"] = "no_silent_window"
    _write_tz_profile(tenant_id, prof)
    return prof


def _write_tz_profile(tenant_id: str, prof: dict[str, Any]) -> None:
    path = tz_profile_path(tenant_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(prof, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# --- the seam every BUY passes through ---------------------------------------

def screen_buy(*, price: float | None, tenant_id: str, entry: dict[str, Any] | None = None,
               now_ts: float | None = None, tz_offset_hours: float | None = None,
               override_reason: str | None = None, context: str = "",
               log: bool = True) -> dict[str, Any]:
    """Run the BUY rules over one intended order.

    `blocked` is true only for a rule in enforce mode with no user override. A
    shadow trip is recorded and lets the order through — that is what shadow is.
    An override never widens a money fuse; it only declines this guard rail, and
    the reason is what gets written down.
    """
    now = float(now_ts if now_ts is not None else _now_ts())
    if tz_offset_hours is None:
        prof = load_tz_profile(tenant_id) or {}
        tz_offset_hours = _to_float(prof.get("offset_hours"))
    lh = local_hour_at(now, tz_offset_hours)

    verdicts = [check_cheap_ticket(price), check_night_lottery(price, lh)]
    tripped: list[str] = []
    blocked_by: list[str] = []
    for v in verdicts:
        v["mode"] = rule_mode(v["rule"])
        if v["mode"] == MODE_OFF:
            v["tripped"] = False
        if not v["tripped"]:
            continue
        tripped.append(v["rule"])
        if v["mode"] == MODE_ENFORCE:
            blocked_by.append(v["rule"])

    override = None
    if blocked_by and override_reason:
        override = str(override_reason)[:280]
    out = {
        "verdicts": verdicts,
        "tripped": tripped,
        "blocked": bool(blocked_by) and override is None,
        "blocked_by": blocked_by,
        "override_reason": override,
        "tz_offset_hours": tz_offset_hours,
        "local_hour": (round(lh, 2) if lh is not None else None),
    }
    if log and tripped:
        log_trap({"event": "trap_screen", "tenant_id": tenant_id, "context": context,
                  "price": price, "tripped": tripped, "blocked": out["blocked"],
                  "blocked_by": blocked_by, "override_reason": override,
                  "local_hour": out["local_hour"], "modes": {v["rule"]: v["mode"] for v in verdicts}})
    return out


def explain(rule: str) -> str:
    """The only explanation somebody sees when an order is intercepted, so it has
    to stand on its own: one fact, one way through.

    Every line states a measured accounting fact about what has happened in a
    price band. None of them predicts, promises, or says "you will lose". The way
    through is deliberate: interception is a guard rail, not a prohibition, and
    somebody who insists can pass with a stated reason, which lands in the audit
    trail and is what makes a later review possible.
    """
    if rule == RULE_CHEAP_TICKET:
        return ("Tickets under 10¢ are where this market's losses concentrate: they are "
                "1.4% of all money deployed, and they lost more than every other trade "
                "combined. Reply with a reason to buy it anyway.")
    if rule == RULE_NIGHT_LOTTERY:
        return ("Sub-20¢ tickets inside this book's declared night window are flagged. "
                "Reply with a reason to buy it anyway.")
    if rule == RULE_ZOMBIE:
        return ("This position is priced at ~$0 — the market has settled the question "
                "and it is only tying up capital. Closing it is yours to do; we never "
                "move a position for you.")
    return ""
