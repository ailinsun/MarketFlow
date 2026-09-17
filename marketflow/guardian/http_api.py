"""Guardian localhost HTTP API — the seam alerts (stdlib, zero-trade-import) uses
to drive hosted onboarding/status/rules/withdrawal without importing the SDK.

Runs inside the guardian venv (it may import the SDK); binds 127.0.0.1 only, with
a shared-token header check so only the co-located alerts process can call it.
Mirrors the bridge live-trading seam: all trade-touching logic stays on this side;
the caller only passes intent + reads results.

Routes (all POST JSON except GET status/exposure):
  POST /guardian/onboard   {chat_id}                 -> disabled legacy endpoint
  GET  /guardian/status?chat_id=..                   -> plan/status/balance/positions
  GET  /guardian/exposure?chat_id=..                 -> read-only event-exposure view
  POST /guardian/rules     {chat_id, stop_loss_pct, take_profit_pct}
  POST /guardian/withdraw  {chat_id, to_address, amount_usd}  -> queue (human-approved)

NEVER exposes a route that signs an order or moves funds directly — exits run in
the resident service tick; withdrawals queue for human approval.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import onboarding as gob  # noqa: E402
from marketflow.guardian import arm as garm  # noqa: E402
from marketflow.guardian import executor as gexec  # noqa: E402
from marketflow.guardian import traps as gtraps  # noqa: E402
from marketflow.guardian import authority as gauth  # noqa: E402
from marketflow.guardian import risk_budget as grisk  # noqa: E402
from marketflow.guardian import turnkey as gturnkey  # noqa: E402

BIND_HOST = os.environ.get("MARKETFLOW_GUARDIAN_HTTP_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("MARKETFLOW_GUARDIAN_HTTP_PORT", "8790"))
# Shared token gates the localhost seam (defence in depth even on loopback).
TOKEN_FILE = os.environ.get(
    "MARKETFLOW_GUARDIAN_HTTP_TOKEN_FILE",
    os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "guardian_http_token.txt"),
)


class _Phase1Session:
    """One-shot, memory-only empty-wallet controller; never used by ``serve``."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.label = f"260812-{int(time.time())}"
        self.tenant_id = "phase1-empty-wallet"
        # Explicitly approved 2026-08-12 API-only-user interoperability rule:
        # API-only users need one non-expiring credential. Capability lifetime is
        # bounded by the exact one-hour policy; final revoke deletes the user.
        self.bootstrap_entry = gturnkey.generate_agent_api_key()
        self.bootstrap_exit = gturnkey.generate_agent_api_key()
        self.entry = gturnkey.generate_agent_api_key()
        self.exit = gturnkey.generate_agent_api_key()
        self.created: dict[str, str] | None = None
        self.agent_user_ids: list[str] = []
        self.policy_ids: list[str] = []
        self.matrix_results: list[dict[str, Any]] = []
        self.unexpected_allow = False
        self.reconcile_evidence: dict[str, Any] | None = None
        self.agent_api_key_ids: dict[str, str] = {}
        self.bootstrap_api_key_ids: dict[str, str] = {}
        self.stale_api_key_ids: dict[str, list[str]] = {"entry": [], "exit": []}
        self.bootstrap_deleted: set[str] = set()
        self.recovery_registered = False
        self.revoked_at: dict[str, float] = {}
        self.revocation_results: list[dict[str, Any]] = []
        self.cancel_all_called = False
        self.root_credential_id = ""
        # Restart-safe Phase 1 resumption. Parent access remains query-only: if
        # the one authorized empty sub-org already exists, attach to its public
        # structure and generate fresh, not-yet-created agent credentials.
        inventory = gturnkey.list_sub_orgs()
        existing = inventory.get("organizationIds") or []
        if len(existing) == 1:
            raw = gturnkey.parent_readonly_snapshot(str(existing[0]))
            root = next((u for u in raw.get("users") or []
                         if u.get("userName") == "owner user root"), None)
            wallet = (raw.get("wallets") or [None])[0]
            account = (raw.get("accounts") or [None])[0]
            auth_rows = raw.get("authenticators") or []
            root_auth_row = next((row for row in auth_rows
                                  if root and row.get("userId") == root.get("userId")), None)
            auths = (root_auth_row.get("authenticators") or []) if root_auth_row else []
            if root and wallet and account and auths:
                self.created = {
                    "suborg_id": str(existing[0]),
                    "wallet_id": str(wallet.get("walletId") or account.get("walletId") or ""),
                    "wallet_address": str(account.get("address") or ""),
                    "root_user_id": str(root.get("userId") or ""),
                }
                root_auth = next(
                    (auth for auth in auths
                     if str(auth.get("authenticatorName") or "") == "owner user-root passkey"),
                    auths[0],
                )
                self.root_credential_id = str(root_auth.get("credentialId") or "")
                self.recovery_registered = len(auths) >= 2
                entry_user = next((u for u in raw.get("users") or []
                                   if u.get("userName") == "MarketFlow entry-agent BUY"), None)
                exit_user = next((u for u in raw.get("users") or []
                                  if u.get("userName") == "MarketFlow exit-agent SELL"), None)
                if entry_user and exit_user:
                    self.agent_user_ids = [
                        str(entry_user.get("userId") or ""),
                        str(exit_user.get("userId") or ""),
                    ]
                    for side, user in (("entry", entry_user), ("exit", exit_user)):
                        keys = user.get("apiKeys") or []
                        self.stale_api_key_ids[side] = [
                            str(key.get("apiKeyId") or "") for key in keys
                            if key.get("apiKeyId")
                        ]
                        bootstrap = next((key for key in keys
                                          if "bootstrap" in str(key.get("apiKeyName") or "")), None)
                        if bootstrap and bootstrap.get("apiKeyId"):
                            self.bootstrap_api_key_ids[side] = str(bootstrap["apiKeyId"])

    def public(self) -> dict[str, Any]:
        return {
            "created": bool(self.created),
            "tenant_id": self.tenant_id,
            "suborg_id": (self.created or {}).get("suborg_id"),
            "wallet_address": (self.created or {}).get("wallet_address"),
            "root_user_id": (self.created or {}).get("root_user_id"),
            "entry_public_key": self.entry["public_key"],
            "exit_public_key": self.exit["public_key"],
            "policy_ttl_seconds": gturnkey.USER_ROOT_AGENT_TTL_SEC,
            "bounded_agent_keys_created": len(self.agent_api_key_ids) == 2,
            "bootstrap_keys_deleted": len(self.bootstrap_deleted) == 2,
            "api_base": gturnkey.API_BASE,
            "users_created": len(self.agent_user_ids) == 2,
            "policies_created": len(self.policy_ids) == 2,
            "matrix_complete": len(self.matrix_results) == 18,
            "unexpected_allow": self.unexpected_allow,
            "reconciled": bool(self.reconcile_evidence),
            "recovery_registered": self.recovery_registered,
            "entry_revoked": "entry" in self.revoked_at,
            "exit_revoked": "exit" in self.revoked_at,
            "cancel_all_called": self.cancel_all_called,
        }

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.created is not None:
            raise gturnkey.TurnkeyRefused("Phase 1 sub-org already created in this session")
        self.created = gturnkey.create_user_root_suborg(
            label=self.label,
            root_user_name="owner user root",
            challenge=str(body.get("challenge") or ""),
            attestation=dict(body.get("attestation") or {}),
        )
        self.root_credential_id = str((body.get("attestation") or {}).get("credentialId") or "")
        return self.public()

    def root_credential(self) -> dict[str, str]:
        if not self.root_credential_id:
            raise gturnkey.TurnkeyRefused("root authenticator was not reconciled")
        return {"credential_id": self.root_credential_id}

    def agent_request(self) -> dict[str, Any]:
        if not self.created:
            raise gturnkey.TurnkeyRefused("create the user-root sub-org first")
        if self.agent_user_ids:
            raise gturnkey.TurnkeyRefused("agent users already exist")
        return {
            "type": "ACTIVITY_TYPE_CREATE_API_ONLY_USERS",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": gturnkey.user_root_api_only_agent_users_parameters(
                entry_public_key=self.entry["public_key"],
                exit_public_key=self.exit["public_key"],
            ),
        }

    def record_agent_users(self, body: dict[str, Any]) -> dict[str, Any]:
        ids = [str(value) for value in body.get("user_ids") or [] if str(value)]
        if len(ids) != 2:
            raise gturnkey.TurnkeyRefused("exactly two delegated user IDs are required")
        self.agent_user_ids = ids
        raw = gturnkey.parent_readonly_snapshot(self.created["suborg_id"])
        by_id = {str(row.get("userId") or ""): row for row in raw.get("users") or []}
        for side, uid in zip(("entry", "exit"), ids):
            keys = (by_id.get(uid) or {}).get("apiKeys") or []
            if len(keys) != 1 or not keys[0].get("apiKeyId"):
                raise gturnkey.TurnkeyRefused("active API key did not reconcile")
            self.agent_api_key_ids[side] = str(keys[0]["apiKeyId"])
            self.stale_api_key_ids[side] = []
            self.bootstrap_deleted.add(side)
        self.bootstrap_entry["private_key"] = ""
        self.bootstrap_exit["private_key"] = ""
        return self.public()

    def bounded_agent_key_request(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in {"entry", "exit"} or len(self.agent_user_ids) != 2:
            raise gturnkey.TurnkeyRefused("agent users must reconcile before key rotation")
        index = 0 if selected == "entry" else 1
        agent = self.entry if selected == "entry" else self.exit
        return {
            "type": "ACTIVITY_TYPE_CREATE_API_KEYS_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {
                "userId": self.agent_user_ids[index],
                "apiKeys": [{
                    "apiKeyName": f"MarketFlow {selected} Phase 1 active",
                    "publicKey": agent["public_key"],
                    "curveType": "API_KEY_CURVE_P256",
                }],
            },
        }

    def record_bounded_agent_key(self, side: str, body: dict[str, Any]) -> dict[str, Any]:
        selected = str(side).lower()
        ids = [str(value) for value in body.get("api_key_ids") or [] if str(value)]
        if selected not in {"entry", "exit"} or len(ids) != 1:
            raise gturnkey.TurnkeyRefused("exactly one active API key is required")
        self.agent_api_key_ids[selected] = ids[0]
        return self.public()

    def delete_bootstrap_request(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in {"entry", "exit"} or selected not in self.agent_api_key_ids:
            raise gturnkey.TurnkeyRefused("active key must exist before bootstrap deletion")
        index = 0 if selected == "entry" else 1
        stale_ids = list(self.stale_api_key_ids.get(selected) or [])
        if not stale_ids:
            raise gturnkey.TurnkeyRefused("stale/bootstrap keys did not reconcile")
        return {
            "type": "ACTIVITY_TYPE_DELETE_API_KEYS",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {
                "userId": self.agent_user_ids[index],
                "apiKeyIds": stale_ids,
            },
        }

    def record_bootstrap_deleted(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in {"entry", "exit"}:
            raise gturnkey.TurnkeyRefused("side must be entry or exit")
        index = 0 if selected == "entry" else 1
        raw = gturnkey.parent_readonly_snapshot(self.created["suborg_id"])
        by_id = {str(row.get("userId") or ""): row for row in raw.get("users") or []}
        keys = (by_id.get(self.agent_user_ids[index]) or {}).get("apiKeys") or []
        remaining = {str(key.get("apiKeyId") or "") for key in keys}
        active_id = self.agent_api_key_ids.get(selected)
        if remaining != {active_id}:
            raise gturnkey.TurnkeyRefused("bootstrap deletion did not reconcile to one active key")
        self.bootstrap_deleted.add(selected)
        self.stale_api_key_ids[selected] = []
        bootstrap = self.bootstrap_entry if selected == "entry" else self.bootstrap_exit
        bootstrap["private_key"] = ""
        return self.public()

    def policy_request(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.created:
            raise gturnkey.TurnkeyRefused("create the user-root sub-org first")
        ids = list(self.agent_user_ids)
        if len(ids) != 2:
            raise gturnkey.TurnkeyRefused("exactly two delegated user IDs are required")
        if len(self.agent_api_key_ids) != 2 or len(self.bootstrap_deleted) != 2:
            raise gturnkey.TurnkeyRefused(
                "active-key rotation must finish before policy creation"
            )
        self.agent_user_ids = ids
        expires = __import__("datetime").datetime.fromtimestamp(
            time.time() + gturnkey.USER_ROOT_AGENT_TTL_SEC,
            tz=__import__("datetime").timezone.utc,
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        policies = [
            gturnkey.user_root_policy_intent(
                tenant_id=self.tenant_id,
                suborg_id=self.created["suborg_id"],
                agent_user_id=ids[0],
                side="BUY",
                maker_amount_ceiling=5_000_000,
                wallet_address=self.created["wallet_address"],
                builder_code=gturnkey.BYTES32_ZERO,
                expires_at=expires,
            ),
            gturnkey.user_root_policy_intent(
                tenant_id=self.tenant_id,
                suborg_id=self.created["suborg_id"],
                agent_user_id=ids[1],
                side="SELL",
                maker_amount_ceiling=5_000_000,
                wallet_address=self.created["wallet_address"],
                builder_code=gturnkey.BYTES32_ZERO,
                expires_at=expires,
            ),
        ]
        return {
            "type": "ACTIVITY_TYPE_CREATE_POLICIES",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {"policies": policies},
        }

    def record_policies(self, body: dict[str, Any]) -> dict[str, Any]:
        ids = [str(value) for value in body.get("policy_ids") or [] if str(value)]
        if len(ids) != 2:
            raise gturnkey.TurnkeyRefused("exactly two policy IDs are required")
        self.policy_ids = ids
        return self.public()

    def _typed_attempt(
        self,
        *,
        name: str,
        expected: str,
        agent: dict[str, str],
        typed_data: dict[str, Any],
        sign_with: str | None = None,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        response = gturnkey.submit_activity(
            "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            {
                "signWith": sign_with or self.created["wallet_address"],
                "payload": json.dumps(typed_data, separators=(",", ":")),
                "encoding": "PAYLOAD_ENCODING_EIP712",
                "hashFunction": "HASH_FUNCTION_NOT_APPLICABLE",
            },
            organization_id=organization_id or self.created["suborg_id"],
            priv_hex=agent["private_key"],
            pub_hex=agent["public_key"],
        )
        activity = response.get("activity") or {}
        allowed = bool((activity.get("result") or {}).get("signRawPayloadResult"))
        observed = "ALLOW" if allowed else "DENY"
        result = {
            "case": name,
            "expected": expected,
            "observed": observed,
            "activity_id_digest": gturnkey._fingerprint(activity.get("id")),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        if expected != observed:
            self.unexpected_allow = observed == "ALLOW"
            raise gturnkey.TurnkeyRefused("live matrix expectation mismatch")
        return result

    def _local_deny(self, name: str, action) -> dict[str, Any]:
        started = time.monotonic()
        try:
            action()
        except gturnkey.TurnkeyRefused:
            return {
                "case": name,
                "expected": "DENY",
                "observed": "DENY",
                "enforcement": "local_context_gate",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        raise gturnkey.TurnkeyRefused("live matrix expectation mismatch")

    def run_matrix(self) -> dict[str, Any]:
        """Run the signed-but-never-submitted empty-wallet matrix once."""
        if not self.created or len(self.policy_ids) != 2:
            raise gturnkey.TurnkeyRefused("policies must exist before the live matrix")
        if len(self.matrix_results) == 18:
            return {"results": self.matrix_results, **self.public()}
        wallet = self.created["wallet_address"]
        suborg = self.created["suborg_id"]
        tenant = self.tenant_id
        cap = 5_000_000
        entry_buy = gturnkey.build_phase1_order_typed_data(
            wallet_address=wallet, side="BUY", maker_amount=1
        )
        exit_sell = gturnkey.build_phase1_order_typed_data(
            wallet_address=wallet, side="SELL", maker_amount=1
        )
        rows: list[dict[str, Any]] = list(self.matrix_results)
        done = {str(row.get("case") or "") for row in rows}

        def add(row: dict[str, Any]) -> None:
            nonlocal done
            rows[:] = [old for old in rows if old.get("case") != row.get("case")]
            rows.append(row)
            done.add(str(row.get("case") or ""))
            self.matrix_results = list(rows)

        if "entry BUY" not in done:
            add(self._typed_attempt(name="entry BUY", expected="ALLOW", agent=self.entry, typed_data=entry_buy))
        if "exit SELL" not in done:
            add(self._typed_attempt(name="exit SELL", expected="ALLOW", agent=self.exit, typed_data=exit_sell))

        def mutated(base: dict[str, Any], *, domain: dict[str, Any] | None = None,
                    message: dict[str, Any] | None = None) -> dict[str, Any]:
            value = json.loads(json.dumps(base))
            if domain:
                value["domain"].update(domain)
            if message:
                value["message"].update(message)
            return value

        wrong_addr = "0x" + "1" * 40
        deny_cases = [
            ("wrong wallet", self.entry, entry_buy, wrong_addr, None),
            ("wrong maker", self.entry, mutated(entry_buy, message={"maker": wrong_addr}), None, None),
            ("wrong signer", self.entry, mutated(entry_buy, message={"signer": wrong_addr}), None, None),
            ("wrong chain", self.entry, mutated(entry_buy, domain={"chainId": 1}), None, None),
            ("wrong domain version", self.entry, mutated(entry_buy, domain={"version": "1"}), None, None),
            ("wrong contract", self.entry, mutated(entry_buy, domain={"verifyingContract": gturnkey.NEG_RISK_CTF_EXCHANGE_V2}), None, None),
            ("wrong signature type", self.entry, mutated(entry_buy, message={"signatureType": 3}), None, None),
            ("wrong side", self.entry, mutated(entry_buy, message={"side": 1}), None, None),
            ("wrong builder", self.entry, mutated(entry_buy, message={"builder": "0x" + "1" * 64}), None, None),
            ("BUY collateral cap+1", self.entry, mutated(entry_buy, message={"makerAmount": cap + 1}), None, None),
            ("SELL share cap+1", self.exit, mutated(exit_sell, message={"makerAmount": cap + 1}), None, None),
            ("wrong sub-org", self.entry, entry_buy, None, "00000000-0000-0000-0000-000000000000"),
        ]
        for name, agent, payload, sign_with, org in deny_cases:
            if name not in done:
                add(self._typed_attempt(name=name, expected="DENY", agent=agent, typed_data=payload,
                                        sign_with=sign_with, organization_id=org))

        # Tenant is a MarketFlow context label, not an EIP-712 or Turnkey policy field;
        # the local context gate is the enforceable seam, while sub-org is enclave-bound.
        if "wrong tenant" not in done:
            add(self._local_deny("wrong tenant", lambda: gturnkey.assert_phase1_order_grant(
                entry_buy, requested_tenant_id="other-tenant", expected_tenant_id=tenant,
                requested_suborg_id=suborg, expected_suborg_id=suborg, side="BUY",
                maker_amount_ceiling=cap, wallet_address=wallet, builder_code=gturnkey.BYTES32_ZERO,
            )))

        batch = {
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                      "Batch": [{"name": "transactions", "type": "bytes"}]},
            "primaryType": "Batch", "domain": {"name": "DepositWallet"},
            "message": {"transactions": "0x"},
        }
        transfer = {
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                      "Transfer": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}]},
            "primaryType": "Transfer", "domain": {"name": "pUSD"},
            "message": {"to": wrong_addr, "amount": 1},
        }
        if "Batch" not in done:
            add(self._typed_attempt(name="Batch", expected="DENY", agent=self.entry, typed_data=batch))
        if "transfer/withdraw" not in done:
            add(self._typed_attempt(name="transfer/withdraw", expected="DENY", agent=self.entry, typed_data=transfer))

        from eth_account._utils.legacy_transactions import serializable_unsigned_transaction_from_dict
        unsigned = serializable_unsigned_transaction_from_dict({
            "nonce": 0, "gasPrice": 1, "gas": 21_000, "to": wallet,
            "value": 1, "data": b"", "chainId": gturnkey.POLYGON_CHAIN_ID,
        })
        if "raw transaction" not in done:
            import rlp
            raw_response = gturnkey.submit_activity(
                "ACTIVITY_TYPE_SIGN_TRANSACTION_V2",
                {"signWith": wallet, "unsignedTransaction": "0x" + rlp.encode(unsigned).hex(),
                 "type": "TRANSACTION_TYPE_ETHEREUM"},
                organization_id=suborg, priv_hex=self.entry["private_key"], pub_hex=self.entry["public_key"],
            )
            raw_activity = raw_response.get("activity") or {}
            raw_allowed = bool((raw_activity.get("result") or {}).get("signTransactionResult"))
            raw_row = {"case": "raw transaction", "expected": "DENY",
                       "observed": "ALLOW" if raw_allowed else "DENY",
                       "activity_id_digest": gturnkey._fingerprint(raw_activity.get("id"))}
            add(raw_row)
            if raw_allowed:
                self.unexpected_allow = True
                raise gturnkey.TurnkeyRefused("live matrix expectation mismatch")
        self.matrix_results = rows
        return {"results": rows, **self.public()}

    def reconcile(self) -> dict[str, Any]:
        if not self.created:
            raise gturnkey.TurnkeyRefused("sub-org does not exist")
        raw = gturnkey.parent_readonly_snapshot(self.created["suborg_id"])
        by_id = {str(row.get("userId") or ""): row for row in raw.get("users") or []}
        if len(self.agent_user_ids) == 2:
            for side, uid in zip(("entry", "exit"), self.agent_user_ids):
                keys = (by_id.get(uid) or {}).get("apiKeys") or []
                if len(keys) == 1 and keys[0].get("apiKeyId"):
                    self.agent_api_key_ids[side] = str(keys[0]["apiKeyId"])
        self.reconcile_evidence = gturnkey.redact_readonly_snapshot(raw)
        return {"evidence": self.reconcile_evidence, **self.public()}

    def recovery_request(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.created:
            raise gturnkey.TurnkeyRefused("sub-org does not exist")
        challenge = str(body.get("challenge") or "")
        attestation = dict(body.get("attestation") or {})
        if not challenge or not attestation:
            raise gturnkey.TurnkeyRefused("recovery passkey attestation is incomplete")
        return {
            "type": "ACTIVITY_TYPE_CREATE_AUTHENTICATORS_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {"userId": self.created["root_user_id"], "authenticators": [{
                "authenticatorName": "owner recovery device Phase 1",
                "challenge": challenge,
                "attestation": attestation,
            }]},
        }

    def record_recovery(self) -> dict[str, Any]:
        self.recovery_registered = True
        return self.public()

    def revoke_request(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in {"entry", "exit"}:
            raise gturnkey.TurnkeyRefused("side must be entry or exit")
        index = 0 if selected == "entry" else 1
        if len(self.agent_user_ids) != 2:
            raise gturnkey.TurnkeyRefused("could not reconcile delegated user")
        return {
            "type": "ACTIVITY_TYPE_DELETE_USERS",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {"userIds": [self.agent_user_ids[index]]},
        }

    def record_revocation(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in {"entry", "exit"}:
            raise gturnkey.TurnkeyRefused("side must be entry or exit")
        self.revoked_at[selected] = time.monotonic()
        return self.public()

    def verify_revocation(self, side: str) -> dict[str, Any]:
        selected = str(side).lower()
        if selected not in self.revoked_at:
            raise gturnkey.TurnkeyRefused("revocation was not recorded")
        wallet = self.created["wallet_address"]
        agent = self.entry if selected == "entry" else self.exit
        order_side = "BUY" if selected == "entry" else "SELL"
        payload = gturnkey.build_phase1_order_typed_data(
            wallet_address=wallet, side=order_side, maker_amount=1
        )
        row = self._typed_attempt(
            name=f"{selected} revoked <=60s", expected="DENY", agent=agent, typed_data=payload
        )
        row["within_60_seconds"] = time.monotonic() - self.revoked_at[selected] <= 60
        if not row["within_60_seconds"]:
            raise gturnkey.TurnkeyRefused("revocation propagation exceeded 60 seconds")
        if selected == "entry":
            self.cancel_all_called = True  # seam only; no CLOB network call
            sell = gturnkey.build_phase1_order_typed_data(
                wallet_address=wallet, side="SELL", maker_amount=1
            )
            self.revocation_results.append(self._typed_attempt(
                name="exit SELL after entry revoke", expected="ALLOW", agent=self.exit, typed_data=sell
            ))
        self.revocation_results.append(row)
        return {"results": self.revocation_results, **self.public()}

    def delete_policies_request(self) -> dict[str, Any]:
        if len(self.policy_ids) != 2:
            raise gturnkey.TurnkeyRefused("exactly two policies are required")
        return {
            "type": "ACTIVITY_TYPE_DELETE_POLICIES",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {"policyIds": list(self.policy_ids)},
        }

    def offline_root_request(self) -> dict[str, Any]:
        from eth_account._utils.legacy_transactions import serializable_unsigned_transaction_from_dict
        wallet = self.created["wallet_address"]
        unsigned = serializable_unsigned_transaction_from_dict({
            "nonce": 0, "gasPrice": 1, "gas": 21_000, "to": wallet,
            "value": 1, "data": b"", "chainId": gturnkey.POLYGON_CHAIN_ID,
        })
        import rlp
        return {
            "type": "ACTIVITY_TYPE_SIGN_TRANSACTION_V2",
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": self.created["suborg_id"],
            "parameters": {"signWith": wallet, "unsignedTransaction": "0x" + rlp.encode(unsigned).hex(),
                           "type": "TRANSACTION_TYPE_ETHEREUM"},
        }

    def proxy_webauthn_activity(self, body: dict[str, Any]) -> dict[str, Any]:
        """Forward one root-stamped activity without persisting replayable data."""
        if not self.created:
            raise gturnkey.TurnkeyRefused("sub-org does not exist")
        raw = body.get("raw")
        stamp = body.get("stamp")
        if not isinstance(raw, str) or not isinstance(stamp, str):
            raise gturnkey.TurnkeyRefused("signed request is incomplete")
        if len(raw) > 1_000_000 or len(stamp) > 100_000:
            raise gturnkey.TurnkeyRefused("signed request is too large")
        try:
            request_body = json.loads(raw)
        except ValueError as exc:
            raise gturnkey.TurnkeyRefused("signed request is invalid JSON") from exc
        kind = str(request_body.get("type") or "")
        routes = {
            "ACTIVITY_TYPE_CREATE_USERS_V4": "create_users",
            "ACTIVITY_TYPE_CREATE_API_ONLY_USERS": "create_api_only_users",
            "ACTIVITY_TYPE_CREATE_API_KEYS_V2": "create_api_keys",
            "ACTIVITY_TYPE_CREATE_POLICIES": "create_policies",
            "ACTIVITY_TYPE_CREATE_AUTHENTICATORS_V2": "create_authenticators",
            "ACTIVITY_TYPE_DELETE_API_KEYS": "delete_api_keys",
            "ACTIVITY_TYPE_DELETE_USERS": "delete_users",
            "ACTIVITY_TYPE_DELETE_POLICIES": "delete_policies",
            "ACTIVITY_TYPE_SIGN_TRANSACTION_V2": "sign_transaction",
        }
        route = routes.get(kind)
        if not route or request_body.get("organizationId") != self.created["suborg_id"]:
            raise gturnkey.TurnkeyRefused("signed request is outside this Phase 1 sub-org")
        request = urllib.request.Request(
            gturnkey.API_BASE + "/public/v1/submit/" + route,
            data=raw.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": gturnkey.USER_AGENT,
                "X-Stamp-Webauthn": stamp,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=gturnkey.HTTP_TIMEOUT_SEC) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                parsed = json.loads(error.read().decode("utf-8", "replace"))
            except ValueError:
                parsed = {}
            return {"_http_status": error.code, "message": parsed.get("message") or "request failed"}


_PHASE1_HTML = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MarketFlow Phase 1 empty wallet</title>
<style>body{font:16px/1.5 system-ui;max-width:760px;margin:40px auto;padding:0 20px;background:#0b1020;color:#edf2ff}button{font:inherit;padding:14px 18px;margin:8px 8px 8px 0;border:0;border-radius:10px;background:#8ea5ff;color:#07102b;font-weight:700}pre{white-space:pre-wrap;background:#141c34;padding:16px;border-radius:12px}.ok{color:#70e7ac}.bad{color:#ff9b9b}</style>
<h1>User-root Phase 1 · empty wallet</h1>
<p>This local one-shot page keeps passkey secrets inside the authenticator. It never displays or stores a full Turnkey stamp, challenge, signature, or replayable request.</p>
<button id="create">1 · Create root passkey and empty sub-org</button>
<button id="agents" disabled>2 · Root creates BUY/SELL agents</button>
<button id="addEntry" disabled>2b · Add active entry key</button>
<button id="addExit" disabled>2c · Add active exit key</button>
<button id="dropEntry" disabled>2d · Delete entry bootstrap</button>
<button id="dropExit" disabled>2e · Delete exit bootstrap</button>
<button id="policies" disabled>3 · Root creates exact policies</button>
<button id="matrix" disabled>4 · Run signed-only ALLOW/DENY matrix</button>
<button id="reconcile" disabled>5 · Parent read-only reconcile</button>
<button id="recovery" disabled>6 · Add recovery device</button>
<button id="revokeEntry" disabled>7 · Recovery device revokes entry</button>
<button id="revokeExit" disabled>8 · Recovery device revokes exit</button>
<button id="offline" disabled>9 · Prepare backend-offline root proof</button>
<pre id="status">Ready.</pre>
<script>
const SESSION = location.pathname.split('/').pop();
const $ = id => document.getElementById(id);
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const unb64u = s => Uint8Array.from(atob(s.replaceAll('-','+').replaceAll('_','/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0));
const descriptor = (id, transports=['internal']) => ({type:'public-key',id:unb64u(id),transports});
const transport = x => ({internal:'AUTHENTICATOR_TRANSPORT_INTERNAL',usb:'AUTHENTICATOR_TRANSPORT_USB',nfc:'AUTHENTICATOR_TRANSPORT_NFC',ble:'AUTHENTICATOR_TRANSPORT_BLE',hybrid:'AUTHENTICATOR_TRANSPORT_HYBRID'})[x];
const local = async (path, body) => {
  const r = await fetch('/phase1/'+SESSION+'/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const j=await r.json(); if(!r.ok) throw Error(j.error||r.status); return j;
};
const show = (s, ok=true) => { $('status').className=ok?'ok':'bad'; $('status').textContent=s; };
async function registerPasskey(name, attachment){
  const challenge=crypto.getRandomValues(new Uint8Array(32));
  const userId=crypto.getRandomValues(new Uint8Array(32));
  const cred=await navigator.credentials.create({publicKey:{rp:{id:location.hostname,name:'MarketFlow Phase 1'},user:{id:userId,name,displayName:name},challenge,pubKeyCredParams:[{type:'public-key',alg:-7}],timeout:300000,attestation:'none',authenticatorSelection:{residentKey:'required',requireResidentKey:true,userVerification:'required',authenticatorAttachment:attachment||undefined}}});
  return {challenge:b64u(challenge),attestation:{credentialId:b64u(cred.rawId),clientDataJson:b64u(cred.response.clientDataJSON),attestationObject:b64u(cred.response.attestationObject),transports:(cred.response.getTransports?cred.response.getTransports():['internal']).map(transport).filter(Boolean)}};
}
async function stamp(body, allowCredentials, hints){
  const bytes=new TextEncoder().encode(body); const digest=await crypto.subtle.digest('SHA-256',bytes);
  const hex=[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');
  let assertion;
  try{assertion=await navigator.credentials.get({publicKey:{challenge:new TextEncoder().encode(hex),allowCredentials:allowCredentials||[],hints:hints||[],timeout:300000,userVerification:'preferred'}})}
  catch(e){throw Error('WEBAUTHN '+(e.name||'Error')+': '+e.message)}
  return JSON.stringify({authenticatorData:b64u(assertion.response.authenticatorData),clientDataJson:b64u(assertion.response.clientDataJSON),credentialId:b64u(assertion.rawId),signature:b64u(assertion.response.signature)});
}
async function turnkey(body, allowCredentials, hints){
  const selected=allowCredentials||[];
  const raw=JSON.stringify(body); const x=await stamp(raw,selected,hints);
  const route=({ACTIVITY_TYPE_CREATE_USERS_V4:'create_users',ACTIVITY_TYPE_CREATE_API_ONLY_USERS:'create_api_only_users',ACTIVITY_TYPE_CREATE_API_KEYS_V2:'create_api_keys',ACTIVITY_TYPE_CREATE_POLICIES:'create_policies',ACTIVITY_TYPE_CREATE_AUTHENTICATORS_V2:'create_authenticators',ACTIVITY_TYPE_DELETE_API_KEYS:'delete_api_keys',ACTIVITY_TYPE_DELETE_USERS:'delete_users',ACTIVITY_TYPE_DELETE_POLICIES:'delete_policies',ACTIVITY_TYPE_SIGN_TRANSACTION_V2:'sign_transaction'}[body.type]);
  if(!route)throw Error('unmapped activity route');
  let j;try{j=await local('turnkey-submit',{raw,stamp:x})}catch(e){throw Error('LOCAL_PROXY: '+e.message)}
  if(j._http_status)throw Error('Turnkey '+route+' HTTP '+j._http_status+': '+(j.message||'request failed')); return j;
}
window.phase1={local,registerPasskey,stamp,turnkey,b64u};
local('state').then(async state=>{if(state.created){const r=await local('root-credential');window.rootCredentialId=r.credential_id;$('create').disabled=true;if(state.users_created){$('agents').disabled=true;$('policies').disabled=state.policies_created;show('Resumed the existing empty sub-org and delegated users through parent read-only reconcile.');}else{$('agents').disabled=false;$('offline').disabled=false;show('Resumed the one existing empty sub-org through parent read-only reconcile.')}}}).catch(()=>{});
$('create').onclick=async()=>{try{show('Touch ID or passkey prompt is opening…');const p=await registerPasskey('owner user root','platform');window.rootCredentialId=p.attestation.credentialId;const state=await local('create',p);p.challenge='';p.attestation=null;$('agents').disabled=false;show('Created one empty Turnkey sub-org. Wallet '+state.wallet_address.slice(0,8)+'…'+state.wallet_address.slice(-6));}catch(e){show(e.message,false)}};
$('agents').onclick=async()=>{try{const req=await local('agent-request',{});const out=await turnkey(req,[descriptor(window.rootCredentialId)]);const ids=out.activity?.result?.createApiOnlyUsersResult?.userIds||[];if(ids.length!==2)throw Error('Turnkey did not return exactly two user IDs');await local('record-agents',{user_ids:ids});$('policies').disabled=false;show('Two policyless API-only users created. Their sole keys are unbounded; capability is bounded by the one-hour exact policies and final user deletion.');}catch(e){show((e.name||'Error')+': '+e.message,false)}};
$('addEntry').onclick=async()=>{try{const req=await local('bounded-key-request',{side:'entry'});const out=await turnkey(req);const ids=out.activity?.result?.createApiKeysResult?.apiKeyIds||[];await local('record-bounded-key',{side:'entry',api_key_ids:ids});$('addExit').disabled=false;show('Active entry key created; both users remain policyless.');}catch(e){show(e.message,false)}};
$('addExit').onclick=async()=>{try{const req=await local('bounded-key-request',{side:'exit'});const out=await turnkey(req);const ids=out.activity?.result?.createApiKeysResult?.apiKeyIds||[];await local('record-bounded-key',{side:'exit',api_key_ids:ids});$('dropEntry').disabled=false;show('Active exit key created. Delete all stale/bootstrap keys before policy.');}catch(e){show(e.message,false)}};
$('dropEntry').onclick=async()=>{try{const req=await local('delete-bootstrap-request',{side:'entry'});const out=await turnkey(req);if(!out.activity?.result?.deleteApiKeysResult)throw Error('entry bootstrap deletion failed');await local('record-bootstrap-deleted',{side:'entry'});$('dropExit').disabled=false;show('Entry bootstrap deleted and reconciled.');}catch(e){show(e.message,false)}};
$('dropExit').onclick=async()=>{try{const req=await local('delete-bootstrap-request',{side:'exit'});const out=await turnkey(req);if(!out.activity?.result?.deleteApiKeysResult)throw Error('exit bootstrap deletion failed');await local('record-bootstrap-deleted',{side:'exit'});$('policies').disabled=false;show('All stale keys deleted. Only the two active policyless keys remain.');}catch(e){show(e.message,false)}};
$('policies').onclick=async()=>{try{const req=await local('policy-request',{});const out=await turnkey(req,[descriptor(window.rootCredentialId)]);const ids=out.activity?.result?.createPoliciesResult?.policyIds||[];if(ids.length!==2)throw Error('Turnkey did not return exactly two policy IDs');await local('record-policies',{policy_ids:ids});$('matrix').disabled=false;show('Two independent exact one-hour policies created. Ready for signature matrix.');}catch(e){show(e.message,false)}};
$('matrix').onclick=async()=>{try{show('Running signed-only matrix; no order will be sent…');const out=await local('run-matrix',{});$('reconcile').disabled=false;show(out.results.map(x=>x.case+': '+x.observed).join('\n'));}catch(e){show('STOP: '+e.message+'. Root revocation is required before any further test.',false)}};
$('reconcile').onclick=async()=>{try{const out=await local('reconcile',{});$('recovery').disabled=false;show('Read-only reconcile complete. Digest '+out.evidence.snapshot_digest);}catch(e){show(e.message,false)}};
$('recovery').onclick=async()=>{try{show('Second-device passkey prompt is opening…');const p=await registerPasskey('owner recovery device');window.recoveryCredentialId=p.attestation.credentialId;const req=await local('recovery-request',p);const out=await turnkey(req,[descriptor(window.rootCredentialId)]);if(!out.activity?.result)throw Error('Recovery authenticator was not created');await local('record-recovery',{});p.challenge='';p.attestation=null;$('revokeEntry').disabled=false;show('Recovery device registered.');}catch(e){show(e.message,false)}};
$('revokeEntry').onclick=async()=>{try{const req=await local('revoke-request',{side:'entry'});const out=await turnkey(req,[descriptor(window.rootCredentialId)]);if(!out.activity?.result?.deleteUsersResult)throw Error('Entry user deletion failed');await local('record-revocation',{side:'entry'});const checked=await local('verify-revocation',{side:'entry'});$('revokeExit').disabled=false;show(checked.results.map(x=>x.case+': '+x.observed).join('\n')+'\ncancel-all seam: called');}catch(e){show(e.message,false)}};
$('revokeExit').onclick=async()=>{try{let req=await local('revoke-request',{side:'exit'});let out=await turnkey(req,[descriptor(window.rootCredentialId)]);if(!out.activity?.result?.deleteUsersResult)throw Error('Exit user deletion failed');await local('record-revocation',{side:'exit'});const checked=await local('verify-revocation',{side:'exit'});req=await local('delete-policies-request',{});out=await turnkey(req,[descriptor(window.rootCredentialId)]);if(!out.activity?.result?.deletePoliciesResult)throw Error('Policy revoke failed');await local('reconcile',{});$('offline').disabled=false;show(checked.results.map(x=>x.case+': '+x.observed).join('\n')+'\nfull agent/policy revoke reconciled');}catch(e){show(e.message,false)}};
$('offline').onclick=async()=>{try{window.offlineRequest=await local('offline-root-request',{});show('Backend request prepared. Stop the MarketFlow controller, then run window.phase1.offlineRootProof() from this loaded page.');}catch(e){show(e.message,false)}};
window.phase1.offlineRootProof=async()=>{const req=window.offlineRequest;if(!req)throw Error('offline request not prepared');const raw=JSON.stringify(req);const x=await stamp(raw,[descriptor(window.rootCredentialId)]);const r=await fetch('https://api.turnkey.com/public/v1/submit/sign_transaction',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json','X-Stamp-Webauthn':x},body:raw});const out=await r.json();const ok=!!out.activity?.result?.signTransactionResult;window.offlineRequest=null;show('backend offline; user root funds-movement shape: '+(ok?'ALLOW':'DENY')+'; signed transaction discarded and never broadcast');return ok};
</script>"""


def _token() -> str | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _phase1_handler(session: _Phase1Session):
    """Build an isolated loopback-only handler for the one-shot controller."""

    prefix = f"/phase1/{session.token}"

    class Phase1Handler(BaseHTTPRequestHandler):
        server_version = "marketflow-phase1/0.1"

        def log_message(self, *args: Any) -> None:
            # Deliberately suppress paths, bodies, WebAuthn material and errors.
            return

        def _send_json(self, code: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0 or size > 1_000_000:
                return {}
            try:
                value = json.loads(self.rfile.read(size).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        def _allowed_origin(self) -> bool:
            origin = str(self.headers.get("Origin") or "")
            return origin in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if path == prefix:
                payload = _PHASE1_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self' https://api.turnkey.com; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == prefix + "/state":
                return self._send_json(200, session.public())
            if path == prefix + "/root-credential":
                return self._send_json(200, session.root_credential())
            return self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if not path.startswith(prefix + "/") or not self._allowed_origin():
                return self._send_json(403, {"error": "forbidden"})
            body = self._read_json()
            action = path[len(prefix) + 1:]
            try:
                if action == "create":
                    return self._send_json(200, session.create(body))
                if action == "agent-request":
                    return self._send_json(200, session.agent_request())
                if action == "record-agents":
                    return self._send_json(200, session.record_agent_users(body))
                if action == "bounded-key-request":
                    return self._send_json(
                        200, session.bounded_agent_key_request(str(body.get("side") or ""))
                    )
                if action == "record-bounded-key":
                    return self._send_json(
                        200,
                        session.record_bounded_agent_key(
                            str(body.get("side") or ""), body
                        ),
                    )
                if action == "delete-bootstrap-request":
                    return self._send_json(
                        200, session.delete_bootstrap_request(str(body.get("side") or ""))
                    )
                if action == "record-bootstrap-deleted":
                    return self._send_json(
                        200, session.record_bootstrap_deleted(str(body.get("side") or ""))
                    )
                if action == "policy-request":
                    return self._send_json(200, session.policy_request(body))
                if action == "record-policies":
                    return self._send_json(200, session.record_policies(body))
                if action == "run-matrix":
                    return self._send_json(200, session.run_matrix())
                if action == "reconcile":
                    return self._send_json(200, session.reconcile())
                if action == "recovery-request":
                    return self._send_json(200, session.recovery_request(body))
                if action == "record-recovery":
                    return self._send_json(200, session.record_recovery())
                if action == "revoke-request":
                    return self._send_json(200, session.revoke_request(str(body.get("side") or "")))
                if action == "record-revocation":
                    return self._send_json(200, session.record_revocation(str(body.get("side") or "")))
                if action == "verify-revocation":
                    return self._send_json(200, session.verify_revocation(str(body.get("side") or "")))
                if action == "delete-policies-request":
                    return self._send_json(200, session.delete_policies_request())
                if action == "offline-root-request":
                    return self._send_json(200, session.offline_root_request())
                if action == "turnkey-submit":
                    return self._send_json(200, session.proxy_webauthn_activity(body))
            except Exception as exc:
                # Never reflect request material or an upstream body to the page.
                return self._send_json(409, {"error": type(exc).__name__})
            return self._send_json(404, {"error": "not found"})

    return Phase1Handler


def _tenant_public_view(chat_id: str) -> dict[str, Any]:
    entry = gstore.get_tenant(chat_id)
    if entry is None:
        return {"enrolled": False}
    rules = entry.get("rules") if isinstance(entry.get("rules"), dict) else {}
    caps = gexec.tenant_caps(entry)
    view = {
        "enrolled": True,
        "status": entry.get("status"),
        "custody_mode": entry.get("custody_mode", "hosted_legacy"),
        "authority_mode": entry.get("authority_mode", "legacy_local"),
        "deposit_address": gob.deposit_address(chat_id),
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


def _tenant_exposure_view(chat_id: str) -> dict[str, Any]:
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
    entry = gstore.get_tenant(chat_id)
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
        return self.headers.get("X-Guardian-Token") == want

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
        if u.path == "/guardian/status":
            chat_id = (parse_qs(u.query).get("chat_id") or [""])[0]
            if not chat_id:
                return self._send(400, {"error": "chat_id required"})
            return self._send(200, _tenant_public_view(chat_id))
        if u.path == "/guardian/exposure":
            chat_id = (parse_qs(u.query).get("chat_id") or [""])[0]
            if not chat_id:
                return self._send(400, {"error": "chat_id required"})
            return self._send(200, _tenant_exposure_view(chat_id))
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
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id:
            return self._send(400, {"error": "chat_id required"})
        try:
            if u.path == "/guardian/onboard":
                if not gob.legacy_hosted_onboarding_enabled():
                    return self._send(409, {
                        "error": "legacy hosted-wallet onboarding is disabled",
                        "required_authority": "turnkey_user_root",
                    })
                gob.onboard(chat_id)
                return self._send(200, _tenant_public_view(chat_id))
            if u.path == "/guardian/rules":
                entry = gstore.get_tenant(chat_id)
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
                # User-set exposure caps (their own risk preference). Stored as
                # requested; FuseCaps clamps to the fat-finger ceiling at use time.
                for k in ("max_total_usd", "max_per_trade_usd", "max_drawdown_usd"):
                    if k in body and body[k] is not None:
                        try:
                            v = float(body[k])
                            if v > 0:
                                rules[k] = v
                        except (TypeError, ValueError):
                            pass
                # Risk profile (260725): a word, not twelve numbers. Unknown values
                # are rejected rather than silently defaulted — a user who typed a
                # profile name should not end up on different settings than they read.
                if body.get("risk_profile") is not None:
                    prof = str(body["risk_profile"]).strip().lower()
                    if prof not in gexec.RISK_PROFILES:
                        return self._send(400, {"error": "unknown risk_profile",
                                                "allowed": sorted(gexec.RISK_PROFILES)})
                    rules["risk_profile"] = prof
                # The user's own timezone, when they state it. Inference from their
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
                return self._send(200, _tenant_public_view(chat_id))
            if u.path == "/guardian/arm":
                entry = gstore.get_tenant(chat_id)
                if entry is None:
                    return self._send(404, {"error": "not enrolled"})
                action = str(body.get("action") or "").strip().lower()
                # The caller (alerts bot) passes user_confirmed=True only after the
                # user's explicit two-step Telegram confirmation.
                if action == "arm":
                    if body.get("user_confirmed") is not True:
                        return self._send(400, {"error": "user confirmation required"})
                    mode = str(body.get("mode") or garm.ARM_MODE_EXIT_ONLY).strip().lower()
                    # entry_flb lets automation BUY, so the confirmation the user
                    # gave must have said so. A generic "arm" confirmation is not
                    # consent to open positions — the bot sends a distinct flag
                    # only after showing the buy-specific wording.
                    if mode == garm.ARM_MODE_ENTRY_FLB and body.get("entry_confirmed") is not True:
                        return self._send(400, {"error": "explicit entry confirmation required"})
                    try:
                        return self._send(200, garm.arm_tenant(entry["tenant_id"], mode=mode))
                    except garm.ArmError as exc:
                        return self._send(409, {"error": str(exc)})
                if action == "disarm":
                    return self._send(200, garm.disarm_tenant(entry["tenant_id"]))
                return self._send(400, {"error": "action must be arm|disarm"})
            if u.path == "/guardian/withdraw":
                entry = gstore.get_tenant(chat_id)
                if entry is None:
                    return self._send(404, {"error": "not enrolled"})
                to_addr = str(body.get("to_address") or "").strip()
                if not to_addr.startswith("0x"):
                    return self._send(400, {"error": "valid to_address required"})
                amt = body.get("amount_usd")
                row = gstore.request_withdrawal(entry["tenant_id"], to_address=to_addr,
                                                amount_usd=float(amt) if amt is not None else None)
                return self._send(200, {"queued": True, "status": row["status"]})
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


def serve_phase1_controller(port: int = 8791) -> int:
    """Run the explicitly invoked, one-shot empty-wallet provisioning page."""
    if BIND_HOST not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Phase 1 controller is loopback-only")
    session = _Phase1Session()
    handler = _phase1_handler(session)
    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
    # The token is only a short-lived localhost CSRF capability. It is not a
    # Turnkey credential and is never written to repo/report/evidence.
    print(f"http://127.0.0.1:{port}/phase1/{session.token}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        session.entry.clear()
        session.exit.clear()
    return 0


if __name__ == "__main__":
    if "--phase1-controller" in sys.argv:
        try:
            phase1_port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8791
        except (IndexError, ValueError):
            raise SystemExit("--port must be an integer")
        sys.exit(serve_phase1_controller(phase1_port))
    sys.exit(serve())
