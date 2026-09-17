#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Settlement lifecycle guard for political markets.

**Why this one first.** The publicly documented blow-ups in this category all
point at the same place. None of them was a wrong directional call; each was a
settlement failure — a single unconfirmed source, a literal condition that
conflicted with the obvious intent, or the named source itself being manipulated.
A wrong direction can be conceded and closed. A settlement accident cannot be
taken back.

**Relationship to the resolution verifier.** That module owns oracle mechanics
and deliberately fails closed on political markets. This one does not touch its
verdict; it adds a lifecycle observation on top. The rules are snapshotted at
entry and compared every cycle afterwards, and any drift raises an alert and an
`incident_hold`.

What it stops is settlement risk with evidence behind it: rule drift, an active
oracle dispute, a single-source resolution that already has a proposal. It is not
a blanket "never touch political markets" — that is a self-imposed gate with no
evidence under it. A market with no incident evidence passes, and whether real
money follows is decided downstream by the capital fuses (arm, caps, kill).

Hard boundary: read-only. It touches no arm state, caps, wallet, key or order
path, and it never writes to the verifier's allowlist. Output is isolated under
runtime/execution/politics_lifecycle/.

Usage:
    python3 marketflow/monitor/politics_lifecycle_guard.py --watch-slug <slug> [--slug ...]
    python3 marketflow/monitor/politics_lifecycle_guard.py --scan     # scan liquid political markets
    python3 marketflow/monitor/politics_lifecycle_guard.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from marketflow.paths import PROJECT_DIR as REPO, runtime_path
OUT_DIR = runtime_path("monitor", "politics_lifecycle")
SNAP_PATH = os.path.join(OUT_DIR, "rule_snapshots.json")
INCIDENT_LOG = os.path.join(OUT_DIR, "incidents.jsonl")
GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "marketflow-politics-guard/0.1 (read-only)"}

POLITICS_RE = re.compile(
    r"election|president|senate|congress|governor|mayor|primary|nominee|impeach|"
    r"cabinet|confirm|resign|pardon|indict|supreme court|parliament|minister|"
    r"signed into law|bill|shutdown|tariff|ceasefire|treaty", re.I)

# These phrases in a rule mean settlement depends on a single external source, or
# leaves disputable discretion in it.
SINGLE_SOURCE_RE = re.compile(
    r"according to|as reported by|as determined by|resolution source|"
    r"official (?:announcement|statement|source)|sole discretion", re.I)
CONSENSUS_RE = re.compile(r"consensus|multiple (?:credible )?sources|majority of", re.I)


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts else time.time(), tz=timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, tries: int = 3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return None


def _plist(v):
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v) if isinstance(v, str) else []
        return out if isinstance(out, list) else []
    except ValueError:
        return []


# --------------------------------------------------------------------------- #
# Pure-function layer: fully covered by the selftest, touches no network.
# --------------------------------------------------------------------------- #
def rule_fingerprint(meta: dict) -> dict:
    """Fingerprint the fields that determine settlement. **Settlement-relevant
    fields only**: a price or volume change must never raise a rule-drift alert."""
    parts = {
        "question": str(meta.get("question") or "").strip(),
        "description": str(meta.get("description") or "").strip(),
        "resolved_by": str(meta.get("resolvedBy") or "").strip().lower(),
        "end_date": str(meta.get("endDate") or "").strip(),
        "outcomes": _plist(meta.get("outcomes")),
    }
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return {"hash": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16], "parts": parts}


def source_profile(description: str) -> dict:
    """Classify the resolution-source structure. A single source is the common
    precondition behind every documented settlement accident in this category."""
    d = str(description or "")
    urls = re.findall(r"https?://[^\s)\]]+", d)
    domains = sorted({urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
                      for u in urls if u})
    return {
        "n_source_urls": len(urls),
        "domains": domains,
        "single_source": bool(SINGLE_SOURCE_RE.search(d)) and not CONSENSUS_RE.search(d),
        "consensus_language": bool(CONSENSUS_RE.search(d)),
    }


def diff_fingerprint(old: dict, new: dict) -> list[str]:
    """Names of the fields that drifted. Empty means no drift."""
    if not old:
        return []
    o, n = old.get("parts") or {}, new.get("parts") or {}
    return sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))


def assess_lifecycle(meta: dict, prev_snap: dict | None, uma: dict | None = None,
                     now: float | None = None) -> dict:
    """Lifecycle verdict (pure function).

    `incident_hold=True` forbids new entry and raises a prompt for manual handling
    of anything already open. This module never closes a position itself.

    `clean` follows actual evidence: no hold and no high-severity reason means
    True. There is no blanket refusal of the category — something is stopped when
    there is evidence, and passes when there is not.
    """
    now = now or time.time()
    fp = rule_fingerprint(meta)
    drift = diff_fingerprint(prev_snap, fp)
    src = source_profile(meta.get("description"))
    statuses = {str(x).strip().lower() for x in _plist(meta.get("umaResolutionStatuses"))}
    onchain = (uma or {}).get("state_label")
    disputed = bool((uma or {}).get("disputed")) or onchain == "disputed" or \
        any("disput" in s for s in statuses)
    proposed = (onchain in ("proposed", "expired") if uma else any("propos" in s for s in statuses))

    end_ts = None
    try:
        end_ts = time.mktime(time.strptime(str(meta.get("endDate"))[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        pass
    hours_to_end = (end_ts - now) / 3600.0 if end_ts else None

    reasons = []
    if drift:
        # A settlement-relevant field changing while a position is open is the
        # precursor to the "literal wording versus evident intent" class of
        # accident.
        reasons.append(f"rule_drift:{'+'.join(drift)}")
    if disputed:
        reasons.append("uma_disputed")
    if src["single_source"]:
        reasons.append("single_source_resolution")
    if src["n_source_urls"] == 0:
        reasons.append("no_named_source")
    if proposed and hours_to_end is not None and hours_to_end > 24:
        # A proposal well before the close is an early proposal, which has
        # produced documented accidents.
        reasons.append("early_proposal")

    hold = bool(drift) or disputed or (src["single_source"] and proposed)
    return {
        "as_of": iso(now),
        "slug": meta.get("slug"),
        "question": meta.get("question"),
        "fingerprint": fp["hash"],
        "rule_drift_fields": drift,
        "uma_phase": "disputed" if disputed else ("proposed" if proposed else "open"),
        "source": src,
        "hours_to_end": round(hours_to_end, 2) if hours_to_end is not None else None,
        "incident_hold": hold,
        "reasons": reasons,
        # Evidence gates the refusal: not clean only on a hold or a high-severity
        # reason; everything else passes.
        "clean": (not hold) and not any(
            r.startswith(("rule_drift", "uma_disputed", "single_source")) for r in reasons),
        "clean_note": "cleared unless there is actual settlement-risk evidence",
        "_snapshot": fp,
    }


# --------------------------------------------------------------------------- #
# IO layer
# --------------------------------------------------------------------------- #
def load_snaps() -> dict:
    try:
        with open(SNAP_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_snaps(d: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = SNAP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, SNAP_PATH)


def log_incident(rec: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(INCIDENT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_market(slug: str) -> dict | None:
    rows = _get(f"{GAMMA}/markets?slug={urllib.parse.quote(slug)}")
    return rows[0] if isinstance(rows, list) and rows else None


def scan_politics(limit: int = 40, min_liq: float = 20000.0) -> list[dict]:
    """Liquid political markets. Watch where the money is: the cost of a
    settlement accident scales with the liquidity in the market."""
    evs = _get(f"{GAMMA}/events?closed=false&limit=120&order=volume24hr&ascending=false")
    out = []
    for ev in evs if isinstance(evs, list) else []:
        title = str(ev.get("title") or "")
        if not POLITICS_RE.search(title):
            continue
        for m in ev.get("markets") or []:
            if float(m.get("liquidityNum") or m.get("liquidity") or 0) < min_liq:
                continue
            out.append(m)
            if len(out) >= limit:
                return out
    return out


def run(slugs: list[str] | None, do_scan: bool) -> int:
    snaps = load_snaps()
    markets = []
    if do_scan:
        markets = scan_politics()
    for s in slugs or []:
        m = fetch_market(s)
        if m:
            markets.append(m)
    if not markets:
        print("[guard] nothing to watch")
        return 0
    holds = 0
    for m in markets:
        slug = m.get("slug") or ""
        prev = snaps.get(slug)
        a = assess_lifecycle(m, prev)
        snaps[slug] = a.pop("_snapshot")
        flag = "HOLD" if a["incident_hold"] else ("watch" if a["reasons"] else "ok")
        if a["incident_hold"]:
            holds += 1
            log_incident(a)
        print(f"  [{flag:5}] {str(a['question'])[:62]:62} "
              f"{a['uma_phase']:9} {','.join(a['reasons'])[:46]}")
    save_snaps(snaps)
    print(f"[guard] watching {len(markets)} political markets | incident_hold {holds}")
    return holds


def selftest() -> int:
    ok = []

    def chk(n, c):
        ok.append((n, bool(c)))
        print(("  PASS  " if c else "  FAIL  ") + n)

    base = {"slug": "will-x-be-confirmed", "question": "Will X be confirmed by the Senate?",
            "description": "Resolves YES if confirmed. According to the official Senate roll call "
                           "at https://senate.gov/votes .",
            "resolvedBy": "0xABC", "endDate": "2026-09-01T00:00:00Z",
            "outcomes": '["Yes","No"]', "umaResolutionStatuses": "[]"}
    a1 = assess_lifecycle(base, None, now=1786000000.0)
    chk("single-source market is not clean", a1["clean"] is False)
    chk("single source is identified", "single_source_resolution" in a1["reasons"])
    chk("first observation reports no drift", a1["rule_drift_fields"] == [])
    snap = a1["_snapshot"]

    # a price or volume change must not trigger drift
    same = dict(base, volumeNum=999999, bestBid=0.42, liquidityNum=1)
    a2 = assess_lifecycle(same, snap, now=1786000000.0)
    chk("price or volume change is not rule drift", a2["rule_drift_fields"] == [] and not a2["incident_hold"])

    # rule text changed -> drift plus hold
    changed = dict(base, description=base["description"] + " Clarification: recess appointments count.")
    a3 = assess_lifecycle(changed, snap, now=1786000000.0)
    chk("rule-text drift is caught", "description" in a3["rule_drift_fields"])
    chk("drift raises incident_hold", a3["incident_hold"] is True)

    # oracle dispute -> hold
    a4 = assess_lifecycle(base, snap, uma={"disputed": True, "state_label": "disputed"},
                          now=1786000000.0)
    chk("oracle dispute raises a hold", a4["incident_hold"] and "uma_disputed" in a4["reasons"])

    # consensus wording -> not a single source
    consensus = dict(base, description="Resolves per a consensus of multiple credible sources: "
                                       "https://a.com https://b.com")
    a5 = assess_lifecycle(consensus, None, now=1786000000.0)
    chk("multi-source consensus is not single source", "single_source_resolution" not in a5["reasons"])
    chk("multiple domains are extracted", len(a5["source"]["domains"]) == 2)

    # no named source -> alert
    a6 = assess_lifecycle(dict(base, description="Resolves YES if it happens."), None,
                          now=1786000000.0)
    chk("rule with no named source is flagged", "no_named_source" in a6["reasons"])

    # early proposal
    a7 = assess_lifecycle(dict(base, umaResolutionStatuses='["proposed"]'), snap,
                          now=1786000000.0)
    chk("a proposal well before close is flagged early_proposal", "early_proposal" in a7["reasons"])

    print("\nselftest: " + ("ALL PASS" if all(c for _, c in ok)
                            else f"{sum(1 for _, c in ok if not c)} FAIL"))
    return 0 if all(c for _, c in ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch-slug", action="append", dest="slugs")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return 0 if run(a.slugs, a.scan or not a.slugs) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
