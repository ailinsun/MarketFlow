"""Guardian loopback HTTP seam for an operator's front end (not included here).

Runs inside the guardian environment (it may import the SDK); binds 127.0.0.1 by
default, and every request must carry the shared token, so only a co-located
process holding the token can call it. All trade-touching logic stays on this
side; the caller passes intent and reads results.

Routes (a mandate is addressed by its tenant id):
  GET  /guardian/health                          -> fleet gates
  GET  /guardian/status?tenant_id=..             -> status, caps, rules, arm, authority
  GET  /guardian/exposure?tenant_id=..           -> read-only event-exposure view
  POST /guardian/rules  {tenant_id, ...}         -> the holder's stop/take, caps, profile
  POST /guardian/arm    {tenant_id, action, ...} -> arm / disarm after explicit confirmation

There is no route that signs an order or moves funds: exits and entries run in
the resident service tick, and nothing in this service can transfer out of a
mandate's wallet.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import onboarding as gob  # noqa: E402
from marketflow.guardian import arm as garm  # noqa: E402
from marketflow.guardian import executor as gexec  # noqa: E402
from marketflow.guardian import traps as gtraps  # noqa: E402
from marketflow.guardian import authority as gauth  # noqa: E402
from marketflow.guardian import risk_budget as grisk  # noqa: E402

BIND_HOST = os.environ.get("MARKETFLOW_GUARDIAN_HTTP_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("MARKETFLOW_GUARDIAN_HTTP_PORT", "8790"))
# Shared token gates the localhost seam (defence in depth even on loopback).
TOKEN_FILE = os.environ.get(
    "MARKETFLOW_GUARDIAN_HTTP_TOKEN_FILE",
    os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "guardian_http_token.txt"),
)


def _token() -> str | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _tenant_public_view(tenant_id: str) -> dict[str, Any]:
    entry = gstore.get_tenant(tenant_id)
    if entry is None:
        return {"enrolled": False}
    rules = entry.get("rules") if isinstance(entry.get("rules"), dict) else {}
    caps = gexec.tenant_caps(entry)
    view = {
        "enrolled": True,
        "status": entry.get("status"),
        "custody_mode": entry.get("custody_mode"),
        "authority_mode": entry.get("authority_mode"),
        "deposit_address": gob.deposit_address(tenant_id),
        "collateral_usd": entry.get("collateral_usd"),
        "rules": {"stop_loss_pct": rules.get("stop_loss_pct"),
                  "take_profit_pct": rules.get("take_profit_pct")},
        "caps": {"max_total_usd": caps.max_total_deploy_usd,
                 "max_per_trade_usd": caps.max_per_trade_usd,
                 "max_drawdown_usd": caps.max_drawdown_usd},
        "risk_profile": gexec.risk_profile(entry),
        "arm": garm.arm_status(entry["tenant_id"]),
        "traps": _trap_view(entry["tenant_id"]),
        "live_enabled_globally": gstore.live_enabled(),
        "entry_enabled_globally": gstore.entry_enabled(),
    }
    if entry.get("authority_mode") == gauth.MODE_TURNKEY_USER_ROOT:
        view["authority"] = gauth.public_status(entry["tenant_id"])
        view["risk_budget"] = grisk.buy_gate(
            entry["tenant_id"], limits=grisk.limits_for_entry(entry),
        )
    return view


def _trap_view(tenant_id: str) -> dict[str, Any]:
    """Trap rule state for this tenant, entirely from disk. The zombie numbers are
    the last tick's — recomputing them here would add a positions round trip to
    every status query, which is the same reason /guardian/exposure is separate."""
    view: dict[str, Any] = {
        "modes": {r: gtraps.rule_mode(r) for r in
                  (gtraps.RULE_CHEAP_TICKET, gtraps.RULE_NIGHT_LOTTERY, gtraps.RULE_ZOMBIE)},
        "tz_profile": gtraps.load_tz_profile(tenant_id),
    }
    try:
        with open(os.path.join(gstore.tenant_dir(tenant_id), "latest.json"), encoding="utf-8") as fh:
            view["zombie"] = (json.load(fh) or {}).get("zombie")
    except (OSError, ValueError):
        view["zombie"] = None
    return view


def _tenant_exposure_view(tenant_id: str) -> dict[str, Any]:
    """Read-only event-exposure view of one mandate's portfolio.

    Deliberately separate from `/guardian/status`: this route makes a network
    round trip to read public positions, and hanging that off status would add
    the latency to every status query. It fails soft — any step that fails
    returns `available: false` with a reason rather than a 500, and no other
    route is affected.

    **It is not wired to any execution gate.** It describes the current state and
    never judges whether a position should be reduced; position limits are a
    money-surface decision made elsewhere.

    `use_gamma=False` is a hard requirement: a hardened deployment grants write
    access only to runtime/guardian, and backfilling metadata would write a
    cache. Only the local feed and the fields the positions already carry are
    used.
    """
    entry = gstore.get_tenant(tenant_id)
    if entry is None:
        return {"available": False, "reason": "not_enrolled"}
    # Positions live on the Deposit Wallet. Never fall back to the signer EOA:
    # it is a different address, and reading it reports an empty or wrong
    # portfolio (same reason as onboarding.deposit_address). No wallet means the
    # honest answer is "none".
    wallet = entry.get("funder_address")
    if not wallet:
        return {"available": False, "reason": "deposit_wallet_not_deployed"}
    try:
        from marketflow.risk import exposure as pex  # noqa: PLC0415  (this route only)
        positions = pex.load_public_wallet(str(wallet))
        if not positions:
            return {"available": True, "n_legs": 0, "note": "no open positions"}
        return {"available": True, **pex.guardian_view(positions, use_gamma=False)}
    except Exception as exc:  # a broken display must never affect custody itself
        return {"available": False, "reason": type(exc).__name__}


class _Handler(BaseHTTPRequestHandler):
    server_version = "marketflow-guardian/0.1"

    def _auth_ok(self) -> bool:
        want = _token()
        if not want:  # no token configured -> refuse (fail-closed)
            return False
        got = str(self.headers.get("X-Guardian-Token") or "")
        return hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8"))

    def _send(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, *a):  # quiet; journald captures stdout if needed
        return

    def do_GET(self) -> None:
        if not self._auth_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path in ("/guardian/status", "/guardian/exposure"):
            tenant_id = (parse_qs(u.query).get("tenant_id") or [""])[0]
            if not tenant_id:
                return self._send(400, {"error": "tenant_id required"})
            try:
                gstore.tenant_dir(tenant_id)
            except gstore.StoreError:
                return self._send(400, {"error": "invalid tenant_id"})
            if u.path == "/guardian/status":
                return self._send(200, _tenant_public_view(tenant_id))
            return self._send(200, _tenant_exposure_view(tenant_id))
        if u.path == "/guardian/health":
            return self._send(200, {
                "ok": True,
                "live_enabled": gstore.live_enabled(),
                "entry_enabled": gstore.entry_enabled(),
                "user_root_enabled": gauth.user_root_enabled(),
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        body = self._read_json()
        tenant_id = str(body.get("tenant_id") or "").strip()
        if not tenant_id:
            return self._send(400, {"error": "tenant_id required"})
        try:
            gstore.tenant_dir(tenant_id)
        except gstore.StoreError:
            return self._send(400, {"error": "invalid tenant_id"})
        try:
            if u.path == "/guardian/rules":
                entry = gstore.get_tenant(tenant_id)
                if entry is None:
                    return self._send(404, {"error": "not enrolled"})
                rules = entry.get("rules") if isinstance(entry.get("rules"), dict) else {}
                for k in ("stop_loss_pct", "take_profit_pct"):
                    if k in body and body[k] is not None:
                        try:
                            v = float(body[k])
                            rules[k] = v if 0.0 < v <= 5.0 else None
                        except (TypeError, ValueError):
                            pass
                # The holder's own exposure caps. Stored as requested; FuseCaps
                # clamps to the fat-finger ceiling at use time.
                for k in ("max_total_usd", "max_per_trade_usd", "max_drawdown_usd"):
                    if k in body and body[k] is not None:
                        try:
                            v = float(body[k])
                            if v > 0:
                                rules[k] = v
                        except (TypeError, ValueError):
                            pass
                # Risk profile: a word, not twelve numbers. Unknown values
                # are rejected rather than silently defaulted -- a holder who named a
                # profile should not end up on different settings than they read.
                if body.get("risk_profile") is not None:
                    prof = str(body["risk_profile"]).strip().lower()
                    if prof not in gexec.RISK_PROFILES:
                        return self._send(400, {"error": "unknown risk_profile",
                                                "allowed": sorted(gexec.RISK_PROFILES)})
                    rules["risk_profile"] = prof
                # The holder's own timezone, when they state it. Inference from their
                # silent window covers everyone else; nothing else is ever assumed.
                if body.get("tz_offset_hours") is not None:
                    try:
                        off = float(body["tz_offset_hours"])
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "tz_offset_hours must be a number"})
                    if not (-12.0 <= off <= 14.0):
                        return self._send(400, {"error": "tz_offset_hours out of range"})
                    rules["tz_offset_hours"] = off
                entry["rules"] = rules
                gstore.upsert_tenant(entry)
                gstore.audit("rules_set", tenant_id=entry["tenant_id"], rules=rules)
                if "tz_offset_hours" in rules:
                    gtraps.refresh_tz_profile(entry["tenant_id"], entry.get("funder_address"),
                                              entry=entry)
                return self._send(200, _tenant_public_view(tenant_id))
            if u.path == "/guardian/arm":
                entry = gstore.get_tenant(tenant_id)
                if entry is None:
                    return self._send(404, {"error": "not enrolled"})
                action = str(body.get("action") or "").strip().lower()
                # The caller passes user_confirmed=True only after the account
                # holder's explicit confirmation.
                if action == "arm":
                    if body.get("user_confirmed") is not True:
                        return self._send(400, {"error": "user confirmation required"})
                    mode = str(body.get("mode") or garm.ARM_MODE_EXIT_ONLY).strip().lower()
                    # entry_allowlisted lets automation BUY, so the confirmation the
                    # holder gave must have said so. A generic "arm" confirmation is
                    # not consent to open positions -- the caller sends a distinct
                    # flag only after showing the buy-specific wording.
                    if mode == garm.ARM_MODE_ENTRY_ALLOWLISTED and body.get("entry_confirmed") is not True:
                        return self._send(400, {"error": "explicit entry confirmation required"})
                    try:
                        return self._send(200, garm.arm_tenant(entry["tenant_id"], mode=mode))
                    except garm.ArmError as exc:
                        return self._send(409, {"error": str(exc)})
                if action == "disarm":
                    return self._send(200, garm.disarm_tenant(entry["tenant_id"]))
                return self._send(400, {"error": "action must be arm|disarm"})
        except Exception as exc:  # never leak a stack to the caller
            return self._send(500, {"error": f"{type(exc).__name__}"})
        return self._send(404, {"error": "not found"})


def serve() -> int:
    httpd = ThreadingHTTPServer((BIND_HOST, BIND_PORT), _Handler)
    print(json.dumps({"guardian_http": f"{BIND_HOST}:{BIND_PORT}", "live_enabled": gstore.live_enabled()}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
