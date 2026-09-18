#!/usr/bin/env python3
"""Real-time Polymarket CLOB price websocket — the cheap, free, always-on monitor.

This is the answer to "why not 1s?": polling a 5-min-stale feed faster gains nothing
because the data only changes every 5 min. The free + truly-real-time source is a
PUSH subscription to the CLOB market websocket: the exchange streams `book` and
`price_change` events as they happen. One persistent connection, zero polling cost.

Each `price_change` item carries `best_bid` + `best_ask` directly, so the mid is exact
without maintaining an order book. We keep a per-token price store
{token_id: {best_bid, best_ask, mid, ts}} that consumers read for real-time jump
detection, reacting within seconds instead of on a 5-minute poll. No idle spin, no
fixed-tick lag.

CONNECTIVITY: direct by default. A deployment that sends venue traffic through its
own egress sets MARKETFLOW_POLYMARKET_PROXY_URL; the supervised loop then tries that
proxy first and falls back to a direct connection if the proxy path drops.

MONEY SAFETY: read-only public market data. Never signs / places / cancels, never
reads secrets, never touches arm-state / caps / kill. It only writes a price file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

WS_HOST = "ws-subscriptions-clob.polymarket.com"
WS_PORT = 443
WS_PATH = "/ws/market"


def _proxy_host_port() -> Optional[tuple[str, int]]:
    """The configured CONNECT proxy (MARKETFLOW_POLYMARKET_PROXY_URL), or None for a
    direct connection. Nothing is assumed when the variable is unset."""
    raw = os.environ.get("MARKETFLOW_POLYMARKET_PROXY_URL", "").strip()
    if raw:
        u = urlparse(raw)
        if u.hostname and u.port:
            return u.hostname, u.port
    return None


PROXY = _proxy_host_port()

_HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR, runtime_path
from marketflow.feeds.rotation import append_jsonl_lines  # noqa: E402  (path-injected repo module)

OUT_DIR = runtime_path("execution", "price_ws")
PRICE_STORE = os.path.join(OUT_DIR, "price_ws_store.json")
MARKET_FEED = runtime_path("feeds", "markets.jsonl")
SCHEMA_VERSION = "polymarket-price-ws-v0.1"
# Pinned to an edge means already decided: a book whose mid sits at or under 2c,
# or at or over 98c, carries no more information.
SETTLED_EDGE = 0.02
# Watchlist lifetime. On expiry the socket is closed deliberately and the
# supervised reconnect path recomputes the list. The behaviour this replaces
# computed the list once at startup and never refreshed it, so a socket that
# stayed up for days kept a days-old list — which is exactly how a stream ends up
# subscribed entirely to markets that have stopped moving.
WATCHLIST_TTL_S = 900.0


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


# --------------------------------------------------------------------------- #
# Pure event -> price store update (unit-tested offline).
# --------------------------------------------------------------------------- #
def _set_price(store: dict, tid: str, bb: Optional[float], ba: Optional[float],
               src_ts_ms: Optional[int] = None) -> None:
    cur = store.get(tid, {})
    bb = bb if bb is not None else cur.get("best_bid")
    ba = ba if ba is not None else cur.get("best_ask")
    mid = None
    if bb is not None and ba is not None:
        mid = (bb + ba) / 2.0
    elif bb is not None:
        mid = bb
    elif ba is not None:
        mid = ba
    entry = {
        "best_bid": bb, "best_ask": ba,
        "mid": round(mid, 6) if mid is not None else None,
        "ts": iso_now(),
        # Venue-side event time (the `timestamp` on the ws event, milliseconds).
        # Without it only the local capture time survives, and this channel's
        # end-to-end latency becomes uncomputable — which matters, because
        # latency is the whole question for any strategy that competes on
        # reaction speed.
        "src_ts_ms": src_ts_ms if isinstance(src_ts_ms, int) else cur.get("src_ts_ms"),
    }
    # Carry forward microstructure depth features: only `book` snapshots refresh
    # them; frequent `price_change` events (best bid/ask only) must not wipe them.
    for k in ("imbalance", "micro_price", "bid_depth", "ask_depth", "micro_ts"):
        if k in cur:
            entry[k] = cur[k]
    store[tid] = entry


def update_from_event(store: dict, ev: dict) -> set:
    """Apply a `book` or `price_change` event to the store. Returns touched token_ids."""
    touched: set = set()
    if not isinstance(ev, dict):
        return touched
    # Venue event time: both `book` and `price_change` carry `timestamp` at the
    # top level of the event, as a millisecond string.
    src_ts_ms: Optional[int] = None
    try:
        raw_ts = ev.get("timestamp")
        if raw_ts is not None:
            src_ts_ms = int(str(raw_ts))
    except Exception:
        src_ts_ms = None
    et = ev.get("event_type")
    if et == "book":
        tid = ev.get("asset_id")
        if tid is None:
            return touched
        bids = ev.get("bids") or []
        asks = ev.get("asks") or []
        bb = max((p for p in (_f(b.get("price")) for b in bids) if p is not None), default=None)
        ba = min((p for p in (_f(a.get("price")) for a in asks) if p is not None), default=None)
        _set_price(store, str(tid), bb, ba, src_ts_ms)
        # Depth/imbalance/micro-price — the microstructure signal (execution.microstructure).
        # Only `book` events carry sizes; price_change events keep best bid/ask only.
        try:
            from marketflow.execution import microstructure as _MS
            feat = _MS.microstructure_features(bids, asks)
            if feat.get("ok"):
                e = store[str(tid)]
                e["imbalance"] = feat["imbalance"]
                e["micro_price"] = feat["micro_price"]
                e["bid_depth"] = feat["bid_depth"]
                e["ask_depth"] = feat["ask_depth"]
                e["micro_ts"] = e.get("ts")
        except Exception:
            pass
        touched.add(str(tid))
    elif et == "price_change":
        for it in (ev.get("price_changes") or []):
            if not isinstance(it, dict):
                continue
            tid = it.get("asset_id")
            if tid is None:
                continue
            bb, ba = _f(it.get("best_bid")), _f(it.get("best_ask"))
            if bb is not None or ba is not None:
                _set_price(store, str(tid), bb, ba, src_ts_ms)
                touched.add(str(tid))
    return touched


def write_store(store: dict, *, path: str = PRICE_STORE) -> None:
    ensure_parent(path)
    payload = {"schema_version": SCHEMA_VERSION, "generated_at": iso_now(), "prices": store}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


# Append-only tick history. write_store keeps ONLY the latest snapshot per token, so the
# A tick-level book stream evaporates as it passes — the stale-window and
# reaction-latency execution edge cannot
# be measured without a persisted history. This appends one row per touched token per event
# batch (real changes only, watchlist-bounded) as the substrate for that analysis.
# Additive: the snapshot and its consumers are unchanged.
# Rotated: TICK_LEDGER holds only the current segment, older ones live under
# TICK_ARCHIVE_ROOT. Full-history consumers must read via rotation.iter_jsonl_lines.
TICK_LEDGER = os.path.join(OUT_DIR, "price_ws_ticks.jsonl")
TICK_ARCHIVE_ROOT = runtime_path("archive", "price-ws-ticks-daily")


def append_ticks(store: dict, touched: set, *, path: str = TICK_LEDGER, ts: Optional[str] = None) -> int:
    """Append (ts, token, bid, ask, mid) for each touched token. Returns rows written."""
    if not touched:
        return 0
    stamp = ts or iso_now()
    now_ms = int(time.time() * 1000)
    rows = []
    for tid in touched:
        rec = store.get(str(tid))
        if not isinstance(rec, dict):
            continue
        row = {"ts": stamp, "token": str(tid),
               "bid": rec.get("best_bid"), "ask": rec.get("best_ask"), "mid": rec.get("mid")}
        # End-to-end latency is computed and persisted here. It cannot be
        # reconstructed later: capture time has only second precision, and the
        # source timestamp is gone the moment it is not written down.
        src = rec.get("src_ts_ms")
        if isinstance(src, int):
            row["ts_source_ms"] = src
            row["lag_ms"] = now_ms - src
        rows.append(row)
    if rows:
        append_jsonl_lines(
            path,
            [json.dumps(r, ensure_ascii=False) + "\n" for r in rows],
            archive_root=TICK_ARCHIVE_ROOT,
        )
    return len(rows)


def load_store(path: str = PRICE_STORE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("prices", {}) if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def store_age_sec(path: str = PRICE_STORE) -> Optional[float]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        ts = d.get("generated_at")
        s = str(ts).replace("Z", "+00:00")
        return time.time() - datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Watchlist: top-N liquid markets' token ids (both YES/NO) from the feed.
# --------------------------------------------------------------------------- #
def _read_last_lines(path: str, n: int) -> list:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max(1, n) * 2000))
            return fh.read().decode("utf-8", "replace").splitlines()[-n:]
    except Exception:
        return []


def _coerce_tokens(v: Any) -> Optional[list]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else None
        except Exception:
            return None
    return None


def watch_tokens(*, n: int = 20, feed_path: str = MARKET_FEED, scan_rows: int = 1500) -> list:
    latest: dict = {}
    for ln in _read_last_lines(feed_path, scan_rows):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        mid = d.get("market_id") or d.get("slug")
        if not mid:
            continue
        if (d.get("updated_ts_ms") or 0) >= (latest.get(mid, {}).get("updated_ts_ms") or 0):
            latest[mid] = d
    now_ms = int(time.time() * 1000)
    rows = []
    for d in latest.values():
        if str(d.get("lifecycle_status") or "").lower() in ("resolved", "closed"):
            continue
        close_ms = d.get("close_ts_ms")
        if isinstance(close_ms, (int, float)) and close_ms <= now_ms:
            continue
        if not d.get("enable_order_book"):
            continue
        # Drop already-decided markets. Measured on a live subscription: every
        # token in it sat at or beyond the 2c / 98c edges, with zero overlap
        # against the thousand-odd tokens that had meaningful volume in the same
        # 24 hours. A decided book carries no information and sampling it is pure
        # idle. Filtering on lifecycle or close time alone does not catch this:
        # a pinned market stays "open" right up to resolution.
        mp = d.get("mid_probability")
        if isinstance(mp, (int, float)) and not (SETTLED_EDGE < mp < 1.0 - SETTLED_EDGE):
            continue
        # Rank by 24h traded volume, not by resting liquidity. A pinned market
        # can show an enormous book and trade nothing. What this channel needs is
        # markets with actual flow: clustering happens in trades, not in quotes.
        rows.append((d.get("volume_24h_usd") or d.get("liquidity_usd") or 0.0, d))
    rows.sort(key=lambda r: r[0], reverse=True)
    tokens: list = []
    for _, d in rows[:n]:
        raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
        toks = d.get("clob_token_ids") or _coerce_tokens(raw.get("clobTokenIds"))
        if isinstance(toks, list):
            tokens.extend(str(t) for t in toks if t)
    # dedup, cap (the ws accepts many but keep it bounded)
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t); out.append(t)
    return out[: max(2, n * 2)]


# --------------------------------------------------------------------------- #
# Async websocket: connect (proxy CONNECT tunnel, direct fallback) -> stream.
# --------------------------------------------------------------------------- #
def _proxy_tunnel_sock(timeout: float = 10.0) -> socket.socket:
    if PROXY is None:
        raise RuntimeError("no proxy configured (MARKETFLOW_POLYMARKET_PROXY_URL)")
    s = socket.create_connection(PROXY, timeout=timeout)
    req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (WS_HOST, WS_PORT, WS_HOST, WS_PORT)
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(256)
        if not chunk:
            s.close()
            raise RuntimeError("proxy closed during CONNECT")
        resp += chunk
    status_line = resp.split(b"\r\n", 1)[0]
    if b" 200 " not in status_line:
        s.close()
        raise RuntimeError("proxy CONNECT failed: " + status_line.decode("replace"))
    s.settimeout(None)
    return s


async def _connect(use_proxy: bool):
    from websockets.legacy.client import connect
    uri = "wss://%s%s" % (WS_HOST, WS_PATH)
    if use_proxy:
        sock = _proxy_tunnel_sock()
        return await connect(uri, sock=sock, server_hostname=WS_HOST,
                             ssl=ssl.create_default_context(), open_timeout=15,
                             ping_interval=20, ping_timeout=15, max_queue=64)
    return await connect(uri, open_timeout=15, ping_interval=20, ping_timeout=15, max_queue=64)


async def stream_once(*, n: int, use_proxy: bool, write_every: float = 5.0) -> None:
    store = load_store()
    tokens = watch_tokens(n=n)
    if not tokens:
        raise RuntimeError("no watch tokens from feed")
    ws = await _connect(use_proxy)
    try:
        await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        print("[price-ws] %s connected (%s) subscribed=%d tokens"
              % (iso_now(), "proxy" if use_proxy else "direct", len(tokens)), file=sys.stderr)
        last_write = 0.0
        started = time.time()
        async for msg in ws:
            # Watchlist expired: close cleanly and let the supervised reconnect
            # recompute the list (see WATCHLIST_TTL_S).
            if time.time() - started >= WATCHLIST_TTL_S:
                print("[price-ws] %s watchlist ttl reached, refreshing" % iso_now(), file=sys.stderr)
                break
            try:
                ev = json.loads(msg)
            except Exception:
                continue
            batch_touched: set = set()
            for e in (ev if isinstance(ev, list) else [ev]):
                batch_touched |= update_from_event(store, e)
            append_ticks(store, batch_touched)        # persist tick history (stale-window substrate)
            now = time.time()
            if now - last_write >= write_every:
                write_store(store)
                last_write = now
    finally:
        write_store(store)
        try:
            await ws.close()
        except Exception:
            pass


async def supervised(*, n: int, write_every: float) -> None:
    """Reconnect forever. With a proxy configured, try it first and alternate with
    DIRECT on repeated failure; without one, always connect directly.
    Exponential-ish backoff, capped."""
    use_proxy = PROXY is not None
    fails = 0
    while True:
        try:
            await stream_once(n=n, use_proxy=use_proxy, write_every=write_every)
            fails = 0
        except Exception as exc:
            fails += 1
            print("[price-ws] %s %s connect/stream failed (#%d): %s"
                  % (iso_now(), "proxy" if use_proxy else "direct", fails, str(exc)[:140]), file=sys.stderr)
            if fails % 3 == 0 and PROXY is not None:
                use_proxy = not use_proxy   # alternate proxy<->direct so either path recovers
            await asyncio.sleep(min(30.0, 2.0 * fails))


# --------------------------------------------------------------------------- #
def selftest() -> dict:
    checks: dict = {}
    store: dict = {}
    book = {"event_type": "book", "asset_id": "tA",
            "bids": [{"price": "0.60", "size": "10"}, {"price": "0.58", "size": "5"}],
            "asks": [{"price": "0.63", "size": "8"}, {"price": "0.65", "size": "4"}]}
    touched = update_from_event(store, book)
    checks["book_best_bid_ask"] = store["tA"]["best_bid"] == 0.60 and store["tA"]["best_ask"] == 0.63
    checks["book_mid"] = abs(store["tA"]["mid"] - 0.615) < 1e-9
    checks["book_touched"] = touched == {"tA"}

    pc = {"event_type": "price_change", "price_changes": [
        {"asset_id": "tA", "price": "0.61", "size": "100", "side": "BUY", "best_bid": "0.61", "best_ask": "0.64"},
        {"asset_id": "tB", "price": "0.20", "size": "50", "side": "SELL", "best_bid": "0.18", "best_ask": "0.22"}]}
    touched = update_from_event(store, pc)
    checks["price_change_updates_mid"] = abs(store["tA"]["mid"] - 0.625) < 1e-9
    checks["price_change_new_token"] = "tB" in store and abs(store["tB"]["mid"] - 0.20) < 1e-9
    checks["price_change_touched_both"] = touched == {"tA", "tB"}

    write_store(store, path=os.path.join(OUT_DIR, "_selftest_price.json"))
    rt = load_store(os.path.join(OUT_DIR, "_selftest_price.json"))
    checks["store_roundtrip"] = rt.get("tA", {}).get("mid") == store["tA"]["mid"]
    try:
        os.remove(os.path.join(OUT_DIR, "_selftest_price.json"))
    except FileNotFoundError:
        pass

    # missing-side price_change keeps the other side
    s2 = {"tC": {"best_bid": 0.40, "best_ask": 0.44, "mid": 0.42, "ts": iso_now()}}
    update_from_event(s2, {"event_type": "price_change",
                           "price_changes": [{"asset_id": "tC", "best_bid": "0.41", "best_ask": "0.45"}]})
    checks["partial_update_keeps_book"] = abs(s2["tC"]["mid"] - 0.43) < 1e-9

    # tick history append: writes one row per touched token with the latest book + mid
    tick_path = os.path.join(OUT_DIR, "_selftest_ticks.jsonl")
    try:
        os.remove(tick_path)
    except FileNotFoundError:
        pass
    nrows = append_ticks(store, {"tA", "tB"}, path=tick_path, ts="2026-06-25T00:00:00Z")
    tick_rows = [json.loads(l) for l in open(tick_path, encoding="utf-8")] if os.path.exists(tick_path) else []
    checks["tick_append_rows"] = nrows == 2 and len(tick_rows) == 2
    checks["tick_append_has_mid"] = all(r.get("mid") is not None and "token" in r for r in tick_rows)
    try:
        os.remove(tick_path)
    except FileNotFoundError:
        pass

    # Source time and latency are persisted: neither can be recovered later, and
    # without both fields this channel's latency is permanently unmeasurable.
    st2 = {}
    update_from_event(st2, {"event_type": "book", "asset_id": "tS", "timestamp": "1785900000000",
                            "bids": [{"price": "0.40", "size": "5"}], "asks": [{"price": "0.42", "size": "5"}]})
    checks["src_ts_parsed"] = st2["tS"].get("src_ts_ms") == 1785900000000
    tick_path2 = tick_path + ".src"
    append_ticks(st2, {"tS"}, path=tick_path2)
    row2 = json.loads(open(tick_path2, encoding="utf-8").read().splitlines()[-1])
    checks["tick_carries_source_and_lag"] = (row2.get("ts_source_ms") == 1785900000000
                                             and isinstance(row2.get("lag_ms"), int))
    try:
        os.remove(tick_path2)
    except FileNotFoundError:
        pass

    # Already-decided markets must be excluded from the watchlist.
    feed_path = tick_path + ".feed"
    far_ms = int(time.time() * 1000) + 86400_000
    with open(feed_path, "w", encoding="utf-8") as fh:
        for mid_p, toks in ((0.995, ["setl_a", "setl_b"]), (0.45, ["live_a", "live_b"])):
            fh.write(json.dumps({"market_id": "m_%s" % mid_p, "updated_ts_ms": 1, "enable_order_book": True,
                                 "lifecycle_status": "open", "close_ts_ms": far_ms, "mid_probability": mid_p,
                                 # a pinned market stays out even with higher volume
                                 "volume_24h_usd": 1000.0 if mid_p > 0.9 else 10.0,
                                 "clob_token_ids": toks}) + "\n")
    wl = watch_tokens(n=5, feed_path=feed_path)
    checks["watchlist_excludes_settled"] = ("setl_a" not in wl and "live_a" in wl)
    try:
        os.remove(feed_path)
    except FileNotFoundError:
        pass

    ok = all(checks.values())
    return {"schema_version": "polymarket-price-ws-selftest-v0.1", "generated_at": iso_now(),
            "PASS": ok, "checks": checks}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Real-time Polymarket CLOB price websocket monitor.")
    ap.add_argument("--selftest", action="store_true", help="Offline selftest (no network).")
    ap.add_argument("--once", action="store_true", help="Connect once for --seconds then exit (live smoke).")
    ap.add_argument("--watch", action="store_true", help="Supervised reconnect loop (the service mode).")
    ap.add_argument("--direct", action="store_true", help="Connect directly even when a proxy is configured.")
    ap.add_argument("--n", type=int, default=20, help="Top-N feed markets to subscribe.")
    ap.add_argument("--seconds", type=float, default=20.0, help="--once duration.")
    ap.add_argument("--write-every", type=float, default=5.0, help="Store flush cadence seconds.")
    args = ap.parse_args(argv)

    if args.selftest:
        rep = selftest()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["PASS"] else 1

    if args.once:
        async def _go():
            try:
                await asyncio.wait_for(stream_once(n=args.n, use_proxy=(PROXY is not None and not args.direct),
                                                   write_every=args.write_every),
                                       timeout=args.seconds)
            except asyncio.TimeoutError:
                pass
        asyncio.run(_go())
        store = load_store()
        print(json.dumps({"tokens_priced": len(store), "sample": dict(list(store.items())[:3])},
                         ensure_ascii=False, indent=2))
        return 0

    if args.watch:
        asyncio.run(supervised(n=args.n, write_every=args.write_every))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
