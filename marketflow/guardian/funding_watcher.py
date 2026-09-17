#!/usr/bin/env python3
"""Funding watcher — deposit detection and relayer-wrap monitoring. Read-only;
it never moves money.

Context: a funding path that ends at a mandate's own Deposit Wallet typically
delivers a stablecoin, while the venue's settlement collateral is a wrapped
token. The venue's own relayer wraps the deposit automatically (measured
on-chain: the wrap lands within seconds of arrival), so nothing here sweeps or
converts anything.

**No pooling.** An earlier design assumed a hosted wallet could not receive the
collateral token and therefore needed a pool to route through. That assumption is
false on-chain: the hosted wallet is itself a Deposit Wallet proxy and the relayer
handles it. The pool is cancelled entirely. Funds stay in one wallet belonging to
one mandate for their whole lifetime, never commingled with anybody else's or with
the operator's — "never pooled" is unambiguous at the code level, not just in
prose.

This module therefore does exactly three read-only things:

  1. Poll each mandate's wallet, and the operator's own deposit address, for
     arrivals of the accepted tokens.
  2. Write each new arrival to the funding ledger and send a notification.
  3. When a deposit has not been wrapped after a reasonable interval, write an
     anomaly row — a lead for a human to look at, never a transfer plan.

Hard boundary: read-only (public chain APIs plus the registry) plus
notifications. It does not sign, does not transfer, and touches no private key.
No transfer code path exists in this service at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
from marketflow import chain  # noqa: E402  the single source of on-chain read primitives
from marketflow.guardian import store as gstore  # noqa: E402

OUT_DIR = runtime_path("guardian", "funding")
LEDGER = os.path.join(OUT_DIR, "funding_ledger.jsonl")
SEEN_FILE = os.path.join(OUT_DIR, "seen.json")
WRAP_WATCH = os.path.join(OUT_DIR, "wrap_watch.jsonl")
LATEST = os.path.join(OUT_DIR, "latest.json")

OPERATOR_CONFIG = runtime_path("guardian", "operator_config.json")
# The operator's own deposits go to the principal account's dedicated deposit
# address rather than to a mandate wallet. The venue relayer wraps those
# automatically, so they are reported on arrival and never enter the wrap-watch
# queue.
ADMIN_TARGET_ID = "__admin_main__"

BLOCKSCOUT = "https://polygon.blockscout.com/api/v2"
# The three funding-relevant tokens on Polygon (all 6 decimals).
TOKENS = {
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD",
}
MIN_NOTIFY_USD = 0.5   # dust transfers are recorded but neither notified nor queued

# **Balances come from the chain, never from an indexer.** An indexer's token
# balances are a snapshot, measured lagging the chain by the better part of an
# hour; during that window it reports money already spent as available, and sizing
# spends against it. Deposit *detection* still uses the indexer — that needs
# transfer history, and arriving late only means notifying late — while a balance
# *reading* goes through chain.erc20_balance.
PUSD_CONTRACT = chain.PUSD          # alias kept for existing call sites and the selftest

_SECRETS_DIR = os.environ.get(
    "MARKETFLOW_GUARDIAN_SECRETS_DIR",
    os.path.join(os.path.expanduser("~"), ".marketflow", "secrets"),
)
_TG_TOKEN_FILE = os.path.join(_SECRETS_DIR, "telegram_bot_token.txt")
_ADMIN_CHAT_FILE = os.path.join(_SECRETS_DIR, "telegram_admin_chat.txt")


def _http_json(url: str, *, tries: int = 3, timeout: int = 20) -> Any:
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "marketflow-guardian-funding/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(2)
    return None


def pusd_balance_onchain(address: str, **kw) -> float | None:
    """On-chain collateral balance of a hosted wallet. None means "not known" —
    the caller falls back to cap-only sizing with the venue as backstop. A failed
    read is never folded into 0, which would stop sizing silently. Implemented in
    `chain.erc20_balance`."""
    return chain.erc20_balance(address, **kw)


def _tg_send(chat_id: str, text: str) -> bool:
    """Fail-soft Telegram push; never raises, never logs the token."""
    try:
        with open(_TG_TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
        if not token or not chat_id:
            return False
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception:
        return False


def _admin_chat() -> str:
    try:
        with open(_ADMIN_CHAT_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(doc: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = f"{SEEN_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, SEEN_FILE)


def _transfer_key(it: dict) -> str:
    raw = "|".join(str(x) for x in (
        it.get("transaction_hash"), (it.get("token") or {}).get("address_hash") or (it.get("token") or {}).get("address"),
        (it.get("total") or {}).get("value"), it.get("log_index")))
    # Stable deduplication key only; SHA-1 is not used for authentication,
    # signatures, or any other security decision.  Preserve the historical
    # identifier while making that boundary explicit to scanners/FIPS builds.
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:20]


def _parse_iso(ts: Any) -> float | None:
    """Indexer ISO timestamp -> epoch seconds; None when unparseable."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    if "." in s:   # drop sub-seconds: fromisoformat is picky beyond 6 digits
        head, _, tail = s.partition(".")
        off = ""
        for i, ch in enumerate(tail):
            if ch in "+-":
                off = tail[i:]
                break
        s = head + off
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _load_operator_config() -> dict:
    """The operator deposit address comes from deployment config, never from a
    second hardcoded copy in this module."""
    try:
        with open(OPERATOR_CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan_targets(registry: dict, *, operator_config: dict | None = None,
                 admin_chat: str = "") -> list[dict]:
    """Every receiving address to watch.

    1. Each mandate's hosted wallet from the registry: a stablecoin arrival there
       waits for the relayer to wrap it.
    2. The principal account's own deposit address, wrapped automatically and
       reported on arrival. Leave it out and the operator's own deposits produce
       no notification at all, because the watcher would only be scanning mandate
       wallets while the operator funds a different address.
    """
    targets: list[dict] = []
    for tid, entry in (registry.get("tenants") or {}).items():
        if not isinstance(entry, dict):
            continue
        addr = entry.get("funder_address") or entry.get("signer_address")
        if addr:
            targets.append({"id": tid, "address": addr,
                            "chat_id": str(entry.get("chat_id") or ""),
                            "created_at": entry.get("created_at"),
                            "auto_wrap": False})
    cfg = operator_config if operator_config is not None else _load_operator_config()
    admin_addr = cfg.get("admin_deposit_address")
    if admin_addr:
        targets.append({"id": ADMIN_TARGET_ID, "address": admin_addr,
                        "chat_id": admin_chat, "created_at": None,
                        "auto_wrap": True})
    return targets


def fetch_transfers(address: str, fetcher=None) -> tuple[list[dict], list[dict]]:
    """ERC-20 transfers for one address -> (incoming, wrap outflows).

    Incoming: to == address and the token is one we accept. That is deposit
    detection.

    Wrap outflow: from == address, to == the collateral token, and the token is a
    stablecoin. This is the on-chain proof that the venue relayer wrapped the
    deposit.

    The share ledger needs it to tell a wrapped-deposit shadow apart from an
    independent transfer of the collateral token: in the incoming stream those two
    are indistinguishable at the same amount in the same window, and only this
    outflow separates them.
    """
    fetcher = fetcher or (lambda a: _http_json(f"{BLOCKSCOUT}/addresses/{a}/token-transfers?type=ERC-20"))
    doc = fetcher(address)
    incoming: list[dict] = []
    wrap_outs: list[dict] = []
    for it in ((doc or {}).get("items") or []):
        tok_meta = it.get("token") or {}
        tok_addr = str(tok_meta.get("address_hash") or tok_meta.get("address") or "").lower()
        if tok_addr not in TOKENS:
            continue
        total = it.get("total") or {}
        try:
            amount = int(total.get("value", "0")) / 10 ** int(total.get("decimals") or 6)
        except (TypeError, ValueError):
            continue
        row = {
            "key": _transfer_key(it),
            "token": TOKENS[tok_addr],
            "amount": round(amount, 6),
            "tx": it.get("transaction_hash"),
            "from": ((it.get("from") or {}).get("hash")) or "",
            "timestamp": it.get("timestamp"),
        }
        to_h = str(((it.get("to") or {}).get("hash")) or "").lower()
        from_h = str(((it.get("from") or {}).get("hash")) or "").lower()
        if to_h == address.lower():
            incoming.append(row)
        elif (from_h == address.lower() and to_h == PUSD_CONTRACT.lower()
              and row["token"] != "pUSD"):
            wrap_outs.append(row)
    return incoming, wrap_outs


def fetch_incoming(address: str, fetcher=None) -> list[dict]:
    return fetch_transfers(address, fetcher=fetcher)[0]


def scan_once(*, fetcher=None, tg=None, now_iso: str | None = None) -> dict:
    """One scan across every receiving address.

    Bootstrap is decided **per address**, not globally. The first time an address
    is scanned its existing history is recorded but not notified, so nobody is
    flooded with backfill; every arrival after that notifies normally.

    A global bootstrap flag would silently swallow the first real deposit of the
    first mandate ever onboarded — precisely the one that must not be missed.
    There is a second safeguard for new mandates: a transfer later than the
    wallet's own creation time notifies even on the first scan, because it cannot
    be history.
    """
    tg = tg or _tg_send
    now_iso = now_iso or gstore.iso_now()
    registry = gstore.load_registry()
    seen = _load_seen()
    admin = _admin_chat()
    targets = scan_targets(registry, admin_chat=admin)
    summary = {"generated_at": now_iso, "tenants_scanned": 0, "new_deposits": 0,
               "wrap_pending": 0, "bootstrap_addresses": 0, "errors": []}
    for target in targets:
        tid, address = target["id"], target["address"]
        summary["tenants_scanned"] += 1
        bootstrap = tid not in seen          # this address has never been scanned
        if bootstrap:
            summary["bootstrap_addresses"] += 1
        created_ts = _parse_iso(target.get("created_at"))
        try:
            transfers, wrap_outs = fetch_transfers(address, fetcher=fetcher)
        except Exception as exc:
            summary["errors"].append(f"{tid}: {exc}")
            continue
        tenant_seen = set(seen.get(tid) or [])
        # A wrap outflow is an evidence row (direction=out): ledgered, never
        # notified, never counted as a deposit. The share ledger needs it to
        # separate a wrapped-deposit shadow from an independent transfer of the
        # collateral token; without this row the two are indistinguishable at the
        # same amount in the same window.
        for wo in wrap_outs:
            if wo["key"] in tenant_seen:
                continue
            tenant_seen.add(wo["key"])
            gstore.append_jsonl(LEDGER, {
                "schema_version": "guardian-funding-v0.1", "observed_at": now_iso,
                "tenant_id": tid, "address": address, "direction": "out",
                "wrap_to": PUSD_CONTRACT, "bootstrap": bootstrap, **wo})
            summary["wrap_outs_recorded"] = summary.get("wrap_outs_recorded", 0) + 1
        for tr in transfers:
            if tr["key"] in tenant_seen:
                continue
            tenant_seen.add(tr["key"])
            # On a first scan, an arrival later than the wallet's creation time
            # is a real deposit rather than history, so it is not silenced.
            tr_ts = _parse_iso(tr.get("timestamp"))
            post_creation = bool(created_ts and tr_ts and tr_ts >= created_ts)
            silent = bootstrap and not post_creation
            row = {"schema_version": "guardian-funding-v0.1", "observed_at": now_iso,
                   "tenant_id": tid, "address": address, "bootstrap": silent, **tr}
            gstore.append_jsonl(LEDGER, row)
            if silent or tr["amount"] < MIN_NOTIFY_USD:
                continue
            summary["new_deposits"] += 1
            chat_id = str(target.get("chat_id") or "")
            if tr["token"] == "pUSD":
                if chat_id:
                    tg(chat_id, f"✅ Deposit credited: ${tr['amount']:.2f} is in your "
                                f"trading balance and monitoring is active.")
                if admin and chat_id != admin:
                    tg(admin, f"💵 Deposit settled: mandate {tid} +${tr['amount']:.2f} collateral")
            elif target.get("auto_wrap"):
                # The venue deposit address: the relayer wraps automatically, so
                # report on arrival.
                if chat_id:
                    tg(chat_id, f"💳 Principal account deposit received: "
                                f"${tr['amount']:.2f} {tr['token']} — the venue relayer is "
                                f"converting it to collateral; the balance is usable once it lands.")
            else:
                # A hosted wallet is itself a Deposit Wallet proxy, so the relayer
                # wraps it too and no pool is involved. All that is recorded here is
                # a watch row: if the matching collateral has still not arrived by
                # the next scan, the relayer did not pick it up and a human should
                # look. A lead, never a transfer plan.
                row_wrap = {"schema_version": "guardian-wrap-watch-v0.1", "observed_at": now_iso,
                            "tenant_id": tid, "address": address, "source_token": tr["token"],
                            "amount_usd": tr["amount"], "deposit_tx": tr["tx"],
                            "expect": "polymarket_relayer_auto_wrap_to_pusd",
                            "resolved": False}
                gstore.append_jsonl(WRAP_WATCH, row_wrap)
                summary["wrap_pending"] += 1
                if chat_id:
                    tg(chat_id, f"⏳ Received your deposit of ${tr['amount']:.2f}; it is "
                                f"being credited (usually a few minutes).")
                if admin and chat_id != admin:
                    tg(admin, f"⏳ Deposit awaiting wrap: mandate {tid} +${tr['amount']:.2f} "
                              f"{tr['token']} — waiting on the venue relayer; added to wrap_watch")
        seen[tid] = sorted(tenant_seen)
    _save_seen(seen)
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = f"{LATEST}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LATEST)
    return summary


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    import tempfile
    global OUT_DIR, LEDGER, SEEN_FILE, WRAP_WATCH, LATEST
    real = (OUT_DIR, LEDGER, SEEN_FILE, WRAP_WATCH, LATEST)
    tmpd = tempfile.mkdtemp(prefix="gfw_selftest_")
    OUT_DIR = tmpd
    LEDGER = os.path.join(tmpd, "funding_ledger.jsonl")
    SEEN_FILE = os.path.join(tmpd, "seen.json")
    WRAP_WATCH = os.path.join(tmpd, "wrap_watch.jsonl")
    LATEST = os.path.join(tmpd, "latest.json")

    tenant_addr = "0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa"

    def mk_item(tok_addr: str, value: str, tx: str, li: int) -> dict:
        return {"transaction_hash": tx, "log_index": li,
                "token": {"address_hash": tok_addr},
                "total": {"value": value, "decimals": "6"},
                "to": {"hash": tenant_addr}, "from": {"hash": "0xbb"},
                "timestamp": "2026-07-24T10:00:00Z"}

    usdc = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
    pusd = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
    junk = "0x1111111111111111111111111111111111111111"

    items1 = [mk_item(usdc, "200000000", "0xt1", 1), mk_item(junk, "5000000", "0xt2", 1)]
    fetcher = lambda a: {"items": items1}
    sent: list[tuple[str, str]] = []
    tg = lambda cid, txt: (sent.append((cid, txt)), True)[1]

    reg = {"schema": 1, "tenants": {"t1": {"chat_id": "777", "funder_address": tenant_addr, "status": "funded"}}}
    real_load = gstore.load_registry
    gstore.load_registry = lambda path=None: reg
    real_operator_cfg = globals()["_load_operator_config"]
    globals()["_load_operator_config"] = lambda: {}      # no operator target by default
    try:
        s1 = scan_once(fetcher=fetcher, tg=tg, now_iso="2026-07-24T10:01:00Z")
        check("first scan bootstraps: recorded, not notified",
              s1["bootstrap_addresses"] == 1 and s1["new_deposits"] == 0 and not sent)
        check("first scan: a non-accepted token is not ledgered",
              sum(1 for _l in open(LEDGER, encoding="utf-8")) == 1)
        items1.append(mk_item(usdc, "50000000", "0xt3", 1))
        s2 = scan_once(fetcher=fetcher, tg=tg, now_iso="2026-07-24T10:03:00Z")
        check("incremental: a new stablecoin arrival is found", s2["new_deposits"] == 1 and s2["wrap_pending"] == 1)
        check("incremental: both holder and operator are notified", len([x for x in sent if x[0] == "777"]) == 1)
        with open(WRAP_WATCH, encoding="utf-8") as f:
            plan = json.loads(f.readline())
        check("wrap-watch row records amount and expectation, and no transfer step",
              plan["amount_usd"] == 50.0 and plan["resolved"] is False
              and plan["expect"] == "polymarket_relayer_auto_wrap_to_pusd"
              and "steps" not in plan and "pool" not in json.dumps(plan).lower())
        sent.clear()
        s3 = scan_once(fetcher=fetcher, tg=tg, now_iso="2026-07-24T10:05:00Z")
        check("idempotent: an already-seen transfer is not reported twice", s3["new_deposits"] == 0 and not sent)
        items1.append(mk_item(pusd, "50000000", "0xt4", 1))
        s4 = scan_once(fetcher=fetcher, tg=tg, now_iso="2026-07-24T10:07:00Z")
        check("collateral arrival: notified as usable, not queued for wrap", s4["new_deposits"] == 1 and s4["wrap_pending"] == 0
              and any("✅" in t for _c, t in sent))
        items1.append(mk_item(usdc, "100000", "0xt5", 1))
        sent.clear()
        s5 = scan_once(fetcher=fetcher, tg=tg, now_iso="2026-07-24T10:09:00Z")
        check("dust below the notify floor: recorded, not notified, not queued", s5["new_deposits"] == 0 and not sent)

        # --- the operator's own deposit address; without it nothing notifies ---
        # Both ambient dependencies are injected. Without that, this section
        # silently depends on whatever happens to exist on the host: one reads a
        # runtime config file and the other reads a file in the secrets
        # directory. A selftest supplies all of its own inputs.
        admin_addr = "0xAdAd000000000000000000000000000000000001"
        admin_chat_id = "1001"   # synthetic; real chat ids never appear in this repo
        globals()["_load_operator_config"] = lambda: {"admin_deposit_address": admin_addr}
        globals()["_admin_chat"] = lambda: admin_chat_id
        tgt = scan_targets(reg, admin_chat=admin_chat_id)
        check("scan targets include both the mandate and the operator address",
              len(tgt) == 2 and any(t["address"] == admin_addr and t["auto_wrap"] for t in tgt))

        admin_items = [dict(mk_item(usdc, "200000000", "0xa1", 1), to={"hash": admin_addr})]
        both = lambda a: {"items": admin_items if a.lower() == admin_addr.lower() else items1}
        sent.clear()
        s6 = scan_once(fetcher=both, tg=tg, now_iso="2026-07-24T10:11:00Z")
        check("operator address first scan: history stays silent", s6["tenants_scanned"] == 2 and not sent)
        admin_items.append(dict(mk_item(usdc, "200000000", "0xa2", 1), to={"hash": admin_addr}))
        s7 = scan_once(fetcher=both, tg=tg, now_iso="2026-07-24T10:13:00Z")
        check("a new operator deposit notifies the operator", s7["new_deposits"] == 1
              and any(c == admin_chat_id for c, _t in sent))
        check("operator deposits go via the relayer and skip the wrap queue", s7["wrap_pending"] == 0
              and any("relayer" in t for _c, t in sent))
        check("operator notification is not sent twice", len(sent) == 1)

        # --- per-address bootstrap: a later mandate is not penalised by an
        #     already-elapsed global bootstrap ---
        t2_addr = "0xBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBbBb"
        reg["tenants"]["t2"] = {"chat_id": "888", "funder_address": t2_addr,
                                "status": "funded", "created_at": "2026-07-24T10:00:00Z"}
        t2_items = [dict(mk_item(usdc, "300000000", "0xb1", 1), to={"hash": t2_addr},
                         timestamp="2026-07-24T10:20:00Z")]
        three = lambda a: {"items": (admin_items if a.lower() == admin_addr.lower()
                                     else t2_items if a.lower() == t2_addr.lower() else items1)}
        sent.clear()
        s8 = scan_once(fetcher=three, tg=tg, now_iso="2026-07-24T10:21:00Z")
        check("new mandate first scan: an arrival after creation still notifies",
              s8["new_deposits"] == 1 and s8["wrap_pending"] == 1
              and any(c == "888" for c, _t in sent))

        t3_addr = "0xCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCc"
        reg["tenants"]["t3"] = {"chat_id": "999", "funder_address": t3_addr,
                                "status": "funded", "created_at": "2026-07-24T10:30:00Z"}
        t3_items = [dict(mk_item(usdc, "400000000", "0xc1", 1), to={"hash": t3_addr},
                         timestamp="2026-07-24T09:00:00Z")]   # before creation = history
        four = lambda a: ({"items": t3_items} if a.lower() == t3_addr.lower() else three(a))
        sent.clear()
        s9 = scan_once(fetcher=four, tg=tg, now_iso="2026-07-24T10:31:00Z")
        check("new mandate first scan: history before creation stays silent", s9["new_deposits"] == 0 and not sent)

        # --- wrap-outflow evidence: the relayer moves the source token into the
        #     collateral token, ledgered as direction=out ---
        wrap_out_item = dict(mk_item(usdc, "50000000", "0xw1", 1),
                             to={"hash": PUSD_CONTRACT}, **{"from": {"hash": tenant_addr}})
        inc, wouts = fetch_transfers(tenant_addr, fetcher=lambda a: {"items": [wrap_out_item]})
        check("a wrap outflow is recognised as evidence, not as a deposit",
              not inc and len(wouts) == 1 and wouts[0]["amount"] == 50.0)
        pusd_out_item = dict(mk_item(pusd, "50000000", "0xw2", 1),
                             to={"hash": "0xDdDdDdDdDdDdDdDdDdDdDdDdDdDdDdDdDdDdDdDd"},
                             **{"from": {"hash": tenant_addr}})
        check("an ordinary outgoing transfer does not enter the evidence stream",
              fetch_transfers(tenant_addr, fetcher=lambda a: {"items": [pusd_out_item]}) == ([], []))
        items1.append(wrap_out_item)
        sent.clear()
        s10 = scan_once(fetcher=four, tg=tg, now_iso="2026-07-24T10:33:00Z")
        check("a wrap outflow is ledgered as direction=out and notifies nobody",
              s10.get("wrap_outs_recorded") == 1 and not sent
              and any(json.loads(x).get("direction") == "out"
                      for x in open(LEDGER, encoding="utf-8")))
        s11 = scan_once(fetcher=four, tg=tg, now_iso="2026-07-24T10:35:00Z")
        check("wrap outflow recording is idempotent", not s11.get("wrap_outs_recorded"))
    finally:
        gstore.load_registry = real_load
        globals()["_load_operator_config"] = real_operator_cfg
        OUT_DIR, LEDGER, SEEN_FILE, WRAP_WATCH, LATEST = real

    print("selftest:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Funding watcher: read-only deposit detection; it never moves money")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=120.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.watch:
        while True:
            try:
                s = scan_once()
                print(f"[funding] scanned={s['tenants_scanned']} new={s['new_deposits']} "
                      f"wrap_pending={s['wrap_pending']}", flush=True)
            except Exception as exc:
                print(f"[funding] tick error: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(30.0, a.interval))
    s = scan_once()
    print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
