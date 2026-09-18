#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public read-only data plane — MCP (streamable HTTP) plus REST.

What it exposes: cleaned aggregates over public market data, settlement-risk
factors, and recent large prints from whatever trade feed the deployment runs.
Execution and risk control are not reachable from here at all.

Hard boundary: read-only. It never touches arm state, caps, keys, secrets or any
execution path. Inputs are allowlist-validated, requests are rate limited per
client address, and upstream metadata is cached.

Bind it to loopback and put a reverse proxy in front; it is not written to face
the internet directly.
"""
from __future__ import annotations

import json
import ipaddress
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Started in Python isolated mode (-I) under a service manager: trust only the
# same-version modules in the read-only deployment directory.
from marketflow.mcp.risk_core import (AppendOnlyStore, WebhookWorker, contract_risk, number,
                       parse_time, portfolio_rollup)

try:
    from marketflow.monitor.settlement_guard import extract_rule_gotchas
except Exception:                           # degrade: no gotchas, service still up
    def extract_rule_gotchas(q, d, max_quotes=2):
        return []

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# Optional local tape written by marketflow.feeds.smart_money. Absent by default;
# the endpoint that reads it degrades rather than failing when it is not set.
WHALE_FEED = os.environ.get("MARKETFLOW_WHALE_FEED", "")
UA = {"User-Agent": "marketflow-data-plane/0.2 (+https://github.com/ailinsun/MarketFlow)"}
# The server binds 127.0.0.1 only (see __main__); the port is the deployment's choice.
PORT = int(os.environ.get("MARKETFLOW_MCP_PORT") or 8791)
MAX_BODY_BYTES = 65_536
MAX_UPSTREAM_BYTES = 4_000_000
MAX_BATCH_REQUESTS = 20

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,120}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# ── Upstream cache (60s) and per-client rate limit (60/min) ─────────────────
_CACHE: dict[str, tuple[float, object]] = {}
_RL: dict[str, list[float]] = {}
_CACHE_LOCK = threading.Lock()
_RL_LOCK = threading.Lock()
_RISK_STORE: AppendOnlyStore | None = None


def risk_store() -> AppendOnlyStore:
    global _RISK_STORE
    if _RISK_STORE is None:
        root = os.environ.get("MARKETFLOW_RISK_STATE_DIR") or os.path.join(
            tempfile.gettempdir(), "marketflow-risk")
        _RISK_STORE = AppendOnlyStore(root)
    return _RISK_STORE


def _rate_ok(ip: str, limit: int = 60, win: float = 60.0) -> bool:
    now = time.time()
    with _RL_LOCK:
        q = [t for t in _RL.get(ip, []) if t > now - win]
        q.append(now)
        _RL[ip] = q
        if len(_RL) > 4096:                 # bound the table
            for k in list(_RL)[:1024]:
                _RL.pop(k, None)
        return len(q) <= limit


def _get_json(url: str, ttl: float = 60.0):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read(MAX_UPSTREAM_BYTES + 1)
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise ValueError("upstream response too large")
    data = json.loads(raw)
    with _CACHE_LOCK:
        _CACHE[url] = (now, data)
        if len(_CACHE) > 512:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:128]:
                _CACHE.pop(k, None)
    return data


def _client_ip(peer: str, forwarded_for: str | None) -> str:
    """Use the right-most address appended by our loopback reverse proxy.

    An arbitrary Internet client may supply the left side of X-Forwarded-For;
    trusting its first value would let it rotate rate-limit identities.
    """
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return "invalid-peer"
    if not peer_ip.is_loopback or not forwarded_for:
        return str(peer_ip)
    for candidate in reversed(forwarded_for.split(",")):
        try:
            return str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            continue
    return str(peer_ip)


def _body_length(raw: str | None) -> tuple[int | None, int | None]:
    if raw is None:
        return 411, None
    try:
        n = int(raw, 10)
    except (TypeError, ValueError):
        return 400, None
    if n < 0:
        return 400, None
    if n > MAX_BODY_BYTES:
        return 413, None
    return None, n


def _market_meta(market: str) -> dict | None:
    """slug or condition_id -> market metadata for one market."""
    market = (market or "").strip()
    if _HEX_RE.match(market):
        rows = _get_json(f"{GAMMA}/markets?condition_ids={market}")
    elif _SLUG_RE.match(market):
        rows = _get_json(f"{GAMMA}/markets?slug={urllib.parse.quote(market)}")
    else:
        return None
    return rows[0] if isinstance(rows, list) and rows else None


def _position_meta(position: dict) -> dict | None:
    condition = str(position.get("conditionId") or "").strip()
    if _HEX_RE.match(condition):
        rows = _get_json(f"{GAMMA}/markets?condition_ids={condition}")
        if isinstance(rows, list) and rows:
            return rows[0]
    slug = str(position.get("slug") or "").strip()
    return _market_meta(slug) if slug else None


def _position_book(position: dict) -> dict | None:
    token = str(position.get("asset") or "").strip()
    if not token.isdigit():
        return None
    doc = _get_json(f"{CLOB}/book?token_id={token}", ttl=8)
    return doc if isinstance(doc, dict) else None


def _wallet_positions(wallet: str) -> list[dict]:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet or ""):
        raise ValueError("wallet must be a 0x address")
    query = urllib.parse.urlencode({"user": wallet, "sizeThreshold": 0.01, "limit": 100,
                                    "sortBy": "CURRENT", "sortDirection": "DESC"})
    rows = _get_json(f"{DATA_API}/positions?{query}", ttl=20)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _risk_for_position(position: dict, *, store: AppendOnlyStore, now: float) -> dict:
    meta = None
    book = None
    try:
        meta = _position_meta(position)
    except Exception:
        pass
    try:
        book = _position_book(position)
    except Exception:
        pass
    return contract_risk(position, meta, book, store=store,
                         gotchas=extract_rule_gotchas, now=now)


def t_portfolio_risk(args: dict) -> dict:
    wallet = str(args.get("wallet") or args.get("address") or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        return {"error": "wallet must be a public 0x address"}
    now = time.time()
    try:
        positions = _wallet_positions(wallet)
    except Exception as exc:
        return {"error": "position source unavailable", "upstream": type(exc).__name__,
                "wallet": wallet, "read_only": True}
    store = risk_store()
    risks: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(positions)))) as pool:
        futures = {pool.submit(_risk_for_position, p, store=store, now=now): i
                   for i, p in enumerate(positions)}
        indexed = []
        for future in as_completed(futures):
            try:
                indexed.append((futures[future], future.result()))
            except Exception as exc:
                indexed.append((futures[future], {"error": type(exc).__name__}))
        for _, result in sorted(indexed):
            if "error" not in result:
                risks.append(result)
    return portfolio_rollup(wallet, positions, risks, now=now, store=store)


def t_market_risk(args: dict) -> dict:
    market = str(args.get("market") or "").strip()
    meta = _market_meta(market)
    if not meta:
        return {"error": "market not found (pass a Polymarket slug or 0x… condition id)"}
    outcomes, prices, tokens = (_plist(meta.get(k)) for k in
                                ("outcomes", "outcomePrices", "clobTokenIds"))
    wanted = str(args.get("outcome") or "").strip().lower()
    index = next((i for i, value in enumerate(outcomes)
                  if str(value).strip().lower() == wanted), 0)
    token = str(tokens[index]) if index < len(tokens) else None
    price = number(prices[index]) if index < len(prices) else None
    position = {"conditionId": meta.get("conditionId"), "slug": meta.get("slug"),
                "eventSlug": meta.get("eventSlug"), "title": meta.get("question"),
                "asset": token, "outcome": outcomes[index] if index < len(outcomes) else None,
                "outcomeIndex": index, "size": 1.0, "curPrice": price,
                "currentValue": price, "initialValue": None,
                "endDate": meta.get("endDate"), "redeemable": False}
    try:
        book = _position_book(position)
    except Exception:
        book = None
    result = contract_risk(position, meta, book, store=risk_store(),
                           gotchas=extract_rule_gotchas)
    result["position"]["synthetic_unit_exposure"] = True
    return result


def t_risk_feed(args: dict) -> dict:
    raw = str(args.get("since") or "").strip()
    since = parse_time(raw) if raw else None
    if raw and since is None:
        return {"error": "since must be an ISO-8601 timestamp or unix timestamp"}
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}
    return risk_store().feed(since, limit)


def _plist(v):
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v) if isinstance(v, str) else []
        return out if isinstance(out, list) else []
    except ValueError:
        return []


# ── tools ────────────────────────────────────────────────────────────────
def t_search_markets(args: dict) -> dict:
    q = str(args.get("query") or "").strip()[:80]
    if len(q) < 2:
        return {"error": "query too short"}
    d = _get_json(f"{GAMMA}/public-search?q={urllib.parse.quote(q)}", ttl=120)
    out = []
    for ev in (d.get("events") or [])[:10]:
        for m in (ev.get("markets") or [])[:3]:
            out.append({"question": m.get("question"), "slug": m.get("slug"),
                        "condition_id": m.get("conditionId"),
                        "end_date": m.get("endDate"),
                        "volume_usd": m.get("volume"),
                        "outcomes": _plist(m.get("outcomes"))})
    return {"query": q, "markets": out[:15]}


def t_market_snapshot(args: dict) -> dict:
    m = _market_meta(str(args.get("market") or ""))
    if not m:
        return {"error": "market not found (pass a Polymarket slug or 0x… condition id)"}
    return {
        "question": m.get("question"), "slug": m.get("slug"),
        "condition_id": m.get("conditionId"),
        "active": m.get("active"), "closed": m.get("closed"),
        "end_date": m.get("endDate"),
        "outcomes": _plist(m.get("outcomes")),
        "outcome_prices": _plist(m.get("outcomePrices")),
        "volume_usd": m.get("volume"), "liquidity_usd": m.get("liquidity"),
        "volume_24h_usd": m.get("volume24hr"),
    }


def t_resolution_risk(args: dict) -> dict:
    """Settlement-risk factors — the public face of the settlement guard.

    Returns the resolution phase (open / proposed / disputed / resolved), who
    resolves it, quoted gotcha clauses lifted out of the resolution text, and the
    time left until close.

    Honest boundary: the metadata API's view of oracle state lags the chain, so
    every response carries that caveat rather than implying it is authoritative.
    A deployment that needs certainty reads the chain directly."""
    m = _market_meta(str(args.get("market") or ""))
    if not m:
        return {"error": "market not found (pass a Polymarket slug or 0x… condition id)"}
    statuses = {str(x).strip().lower() for x in _plist(m.get("umaResolutionStatuses"))}
    disputed = any("disput" in s for s in statuses)
    proposed = any("propos" in s for s in statuses)
    resolved = bool(m.get("closed")) or any("resolved" in s for s in statuses)
    phase = ("disputed" if disputed else "proposed" if proposed
             else "resolved" if resolved else "open")
    gotchas = extract_rule_gotchas(m.get("question"), m.get("description"), max_quotes=3)
    end = m.get("endDate")
    notes = []
    if disputed:
        notes.append("resolution is under UMA dispute — outcome can flip; exits before "
                     "settlement carry dispute risk")
    elif proposed:
        notes.append("an outcome has been proposed on UMA — challenge window may be live")
    if gotchas:
        notes.append("resolution rules contain non-obvious clauses (see rule_gotchas)")
    if not notes:
        notes.append("no elevated resolution flags in public metadata")
    return {
        "question": m.get("question"), "slug": m.get("slug"),
        "phase": phase, "uma_statuses": sorted(statuses),
        "resolved_by": m.get("resolvedBy"),
        "end_date": end, "rule_gotchas": gotchas, "risk_notes": notes,
        "caveat": ("public-metadata view; UMA statuses can lag the chain by minutes. "
                   "On-chain-authoritative reads are part of the paid plane."),
    }


def t_whale_prints(args: dict) -> dict:
    """Recent large prints from the configured trade feed."""
    want = str(args.get("market") or "").strip().lower()
    try:
        limit = max(1, min(int(args.get("limit") or 20), 20))
    except (TypeError, ValueError):
        return {"error": "limit must be an integer from 1 to 20"}
    out = []
    if not WHALE_FEED:
        return {"error": "no trade feed configured; set MARKETFLOW_WHALE_FEED"}
    try:
        size = os.path.getsize(WHALE_FEED)
        with open(WHALE_FEED, "rb") as fh:
            fh.seek(max(0, size - 6_000_000))       # last ~6MB covers the recent window
            tail = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail[1:]):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if want and want not in str(d.get("slug") or "").lower():
                continue
            out.append({"ts": d.get("ts_source_ms"), "market": d.get("title") or d.get("slug"),
                        "slug": d.get("slug"), "side": d.get("side"),
                        "outcome": d.get("outcome"),
                        "price": d.get("price"),
                        "notional_usd": round(float(d.get("notional_usd") or 0))})
            if len(out) >= limit:
                break
    except OSError:
        return {"error": "whale feed temporarily unavailable"}
    return {"prints": out,
            "note": "large prints only; wallet identities are part of the paid plane"}


TOOLS = {
    "search_markets": {
        "fn": t_search_markets,
        "description": "Search Polymarket prediction markets by free text. Returns question, slug, condition id, end date, volume, outcomes.",
        "schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "free-text search, e.g. 'Fed September'"}},
            "required": ["query"]},
    },
    "market_snapshot": {
        "fn": t_market_snapshot,
        "description": "Snapshot of one Polymarket market: prices, volume, liquidity, end date. Pass a slug or 0x… condition id.",
        "schema": {"type": "object", "properties": {
            "market": {"type": "string", "description": "Polymarket slug or condition id"}},
            "required": ["market"]},
    },
    "resolution_risk": {
        "fn": t_resolution_risk,
        "description": "MarketFlow-exclusive settlement/resolution risk read for a Polymarket market: UMA phase (open/proposed/disputed/resolved), resolver, non-obvious rule clauses ('gotchas'), and risk notes. Use before entering or exiting near settlement.",
        "schema": {"type": "object", "properties": {
            "market": {"type": "string", "description": "Polymarket slug or condition id"}},
            "required": ["market"]},
    },
    "whale_prints": {
        "fn": t_whale_prints,
        "description": "MarketFlow's own live whale-print feed for Polymarket (large trades). Optional market filter. Free tier returns the latest 20.",
        "schema": {"type": "object", "properties": {
            "market": {"type": "string", "description": "optional slug substring filter"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "required": []},
    },
}

SERVER_INFO = {"name": "marketflow-prediction-markets", "version": "0.1.0"}
PROTOCOL = "2025-03-26"


def mcp_dispatch(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": ("MarketFlow free data plane for prediction markets. "
                             "resolution_risk and whale_prints are MarketFlow-exclusive; "
                             "market data proxies Polymarket public APIs. Read-only.")}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": k, "description": v["description"], "inputSchema": v["schema"]}
            for k, v in TOOLS.items()]}}
    if method == "tools/call":
        p = msg.get("params") or {}
        tool = TOOLS.get(str(p.get("name")))
        if not tool:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": "unknown tool"}}
        try:
            res = tool["fn"](p.get("arguments") or {})
        except Exception as e:              # never leak a stack trace to a client
            res = {"error": f"internal: {type(e).__name__}"}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
            "isError": "error" in res}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


class H(BaseHTTPRequestHandler):
    server_version = "marketflow-data-plane/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _ip(self) -> str:
        return _client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))

    def _send(self, code: int, obj, ctype="application/json"):
        body = (json.dumps(obj, ensure_ascii=False) if not isinstance(obj, (bytes, str))
                else obj)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id, Accept")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):        # one line per request, for the journal
        sys.stderr.write("%s %s\n" % (self._ip(), fmt % a))

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if not _rate_ok(self._ip()):
            return self._send(429, {"error": "rate limited (60/min free tier)"})
        u = urllib.parse.urlparse(self.path)
        try:
            q = urllib.parse.parse_qs(u.query, max_num_fields=16)
        except ValueError:
            return self._send(400, {"error": "too many query fields"})
        one = {k: v[0] for k, v in q.items()}
        if u.path in ("/healthz", "/v1/healthz"):
            return self._send(200, {"ok": True, "service": SERVER_INFO})
        if u.path == "/v1/risk/portfolio":
            result = t_portfolio_risk(one)
            status = (503 if result.get("error") == "position source unavailable"
                      else 400 if "error" in result else 200)
            return self._send(status, result)
        if u.path == "/v1/risk/market":
            result = t_market_risk(one)
            return self._send(404 if "error" in result else 200, result)
        if u.path == "/v1/risk/feed":
            result = t_risk_feed(one)
            return self._send(400 if "error" in result else 200, result)
        if u.path == "/v1/resolution-risk":
            return self._send(200, t_resolution_risk(one))
        if u.path == "/v1/market":
            return self._send(200, t_market_snapshot(one))
        if u.path == "/v1/whale-prints":
            return self._send(200, t_whale_prints(one))
        if u.path == "/v1/search":
            return self._send(200, t_search_markets(one))
        if u.path == "/mcp":
            return self._send(405, {"error": "POST JSON-RPC to this endpoint"})
        return self._send(404, {"error": "not found", "endpoints": [
            "/v1/risk/portfolio?wallet=", "/v1/risk/market?market=",
            "/v1/risk/feed?since=",
            "/v1/search?query=", "/v1/market?market=", "/v1/resolution-risk?market=",
            "/v1/whale-prints?market=", "POST /mcp"]})

    def do_POST(self):
        if not _rate_ok(self._ip()):
            return self._send(429, {"error": "rate limited (60/min free tier)"})
        u = urllib.parse.urlparse(self.path)
        if u.path != "/mcp":
            return self._send(404, {"error": "not found"})
        length_error, n = _body_length(self.headers.get("Content-Length"))
        if length_error:
            return self._send(length_error, {"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32600,
                                                       "message": "invalid request length"}})
        try:
            msg = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, TimeoutError):
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        if isinstance(msg, list):           # batch
            if len(msg) > MAX_BATCH_REQUESTS:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32600,
                                                  "message": "batch too large"}})
            out = [r for r in (mcp_dispatch(m) for m in msg if isinstance(m, dict)) if r]
            return self._send(200, out) if out else self._send(202, b"")
        resp = mcp_dispatch(msg if isinstance(msg, dict) else {})
        if resp is None:
            return self._send(202, b"")
        return self._send(200, resp)


def selftest() -> None:
    ok = []
    r = mcp_dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    ok.append(("initialize", r["result"]["serverInfo"]["name"] == "marketflow-prediction-markets"))
    r = mcp_dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    ok.append(("tools/list = 4", len(r["result"]["tools"]) == 4))
    r = mcp_dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "nope"}})
    ok.append(("unknown tool -> error", "error" in r))
    ok.append(("notification -> None",
               mcp_dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None))
    ok.append(("slug validation rejects traversal", _market_meta("../../etc/passwd") is None))
    ok.append(("negative body length rejected", _body_length("-1") == (400, None)))
    ok.append(("oversize body rejected", _body_length(str(MAX_BODY_BYTES + 1)) == (413, None)))
    ok.append(("proxy spoof takes right-most IP",
               _client_ip("127.0.0.1", "198.51.100.8, 203.0.113.9") == "203.0.113.9"))
    ok.append(("non-proxy ignores forwarded IP",
               _client_ip("192.0.2.4", "203.0.113.9") == "192.0.2.4"))
    for name, passed in ok:
        print(("PASS " if passed else "FAIL ") + name)
    if not all(p for _, p in ok):
        sys.exit(1)
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    store = risk_store()
    WebhookWorker(store).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    server.daemon_threads = True
    server.serve_forever()
