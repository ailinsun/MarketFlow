"""Offline Phase 1 user-root provisioning/reconcile/DENY regression matrix.

No network, no real credential, no gate and no runtime/registry writes.
"""

from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import turnkey as tk  # noqa: E402
from marketflow.guardian import http_api as hapi  # noqa: E402


def selftest() -> dict[str, bool]:
    checks: dict[str, bool] = {}
    tenant = "phase1-empty-wallet"
    suborg = "suborg-phase1"
    wallet = "0xAbC0000000000000000000000000000000000123"
    builder = tk.BYTES32_ZERO
    buy_cap = 5_000_000
    sell_cap = 7_000_000

    entry = tk.generate_agent_api_key()
    exit_ = tk.generate_agent_api_key()
    params = tk.user_root_api_only_agent_users_parameters(
        entry_public_key=entry["public_key"],
        exit_public_key=exit_["public_key"],
    )
    checks["provisioning_has_exactly_two_nonroot_agents"] = (
        len(params["apiOnlyUsers"]) == 2
        and all(len(row["apiKeys"]) == 1 for row in params["apiOnlyUsers"])
    )
    checks["sole_agent_credentials_are_unbounded"] = all(
        "expirationSeconds" not in row["apiKeys"][0]
        for row in params["apiOnlyUsers"]
    )
    try:
        tk.user_root_api_only_agent_users_parameters(
            entry_public_key=entry["public_key"],
            exit_public_key=exit_["public_key"],
            expiration_seconds=3600,
        )
        checks["expiring_sole_agent_credentials_refused"] = False
    except tk.TurnkeyError:
        checks["expiring_sole_agent_credentials_refused"] = True
    bootstrap = tk.user_root_bootstrap_agent_users_parameters(
        entry_public_key=entry["public_key"], exit_public_key=exit_["public_key"]
    )
    checks["bootstrap_exception_is_explicit_and_policyless"] = (
        len(bootstrap["apiOnlyUsers"]) == 2
        and all("expirationSeconds" not in row["apiKeys"][0]
                for row in bootstrap["apiOnlyUsers"])
        and "policies" not in bootstrap
    )
    checks["relative_expiration_semantics"] = (
        tk.api_key_expiration_seconds(ttl_seconds=3600) == "3600"
    )
    checks["delete_user_activity_is_mapped"] = (
        tk._ACTIVITY_ROUTES.get("ACTIVITY_TYPE_DELETE_USERS") == "delete_users"
    )
    session = hapi._Phase1Session.__new__(hapi._Phase1Session)
    session.created = {"suborg_id": suborg, "wallet_address": wallet}
    session.agent_user_ids = ["entry-user", "exit-user"]
    entry_revoke = hapi._Phase1Session.revoke_request(session, "entry")
    exit_revoke = hapi._Phase1Session.revoke_request(session, "exit")
    checks["final_revoke_deletes_agent_users"] = (
        entry_revoke["type"] == "ACTIVITY_TYPE_DELETE_USERS"
        and entry_revoke["parameters"] == {"userIds": ["entry-user"]}
        and exit_revoke["parameters"] == {"userIds": ["exit-user"]}
    )
    session.created["wallet_address"] = "0x" + "1" * 40
    offline = hapi._Phase1Session.offline_root_request(session)
    checks["offline_root_request_is_rlp_transaction"] = (
        offline["type"] == "ACTIVITY_TYPE_SIGN_TRANSACTION_V2"
        and offline["parameters"]["signWith"] == "0x" + "1" * 40
        and offline["parameters"]["unsignedTransaction"].startswith("0x")
        and len(offline["parameters"]["unsignedTransaction"]) > 20
    )
    source = open(hapi.__file__, encoding="utf-8").read()
    checks["controller_has_one_phase1_handler"] = (
        source.count("def _phase1_handler(session: _Phase1Session):") == 1
    )
    checks["hybrid_recovery_transport_supported"] = (
        "hints:hints||[]" in source
        and "AUTHENTICATOR_TRANSPORT_HYBRID" in source
    )
    checks["agent_private_keys_not_in_root_intent"] = all(
        entry["private_key"] not in json.dumps(params)
        and exit_["private_key"] not in json.dumps(params)
        for _ in (0,)
    )

    buy_condition = tk.user_root_order_policy_condition(
        side="BUY", maker_amount_ceiling=buy_cap, wallet_address=wallet,
        builder_code=builder,
    )
    sell_condition = tk.user_root_order_policy_condition(
        side="SELL", maker_amount_ceiling=sell_cap, wallet_address=wallet,
        builder_code=builder,
    )
    exact_tokens = (
        "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2", "PAYLOAD_ENCODING_EIP712",
        "primary_type == 'Order'", "domain.version == '2'",
        "domain.chain_id == 137", "domain.verifying_contract",
        "message['maker']", "message['signer']", "message['signatureType'] == 0",
        "message['builder']", "wallet_account.address",
    )
    checks["policies_pin_full_v2_identity"] = all(
        token in buy_condition and token in sell_condition for token in exact_tokens
    )
    checks["buy_and_sell_are_separate_policies"] = (
        "message['side'] == 0" in buy_condition
        and "message['side'] == 1" in sell_condition
        and f"makerAmount'] <= {buy_cap}" in buy_condition
        and f"makerAmount'] <= {sell_cap}" in sell_condition
    )
    checks["policy_has_no_builder_fee_field"] = (
        "fee" not in buy_condition.lower() and "fee" not in sell_condition.lower()
    )

    base_buy = tk.build_phase1_order_typed_data(
        wallet_address=wallet, side="BUY", maker_amount=buy_cap, builder_code=builder,
        timestamp_ms=1_786_500_000_000,
    )
    base_sell = tk.build_phase1_order_typed_data(
        wallet_address=wallet, side="SELL", maker_amount=sell_cap, builder_code=builder,
        timestamp_ms=1_786_500_000_001,
    )

    def allowed(payload, *, side="BUY", cap=buy_cap, tenant_value=tenant,
                suborg_value=suborg, wallet_value=wallet, contract=tk.PHASE1_EXCHANGE_CONTRACT,
                builder_value=builder) -> bool:
        try:
            tk.assert_phase1_order_grant(
                payload,
                requested_tenant_id=tenant_value,
                expected_tenant_id=tenant,
                requested_suborg_id=suborg_value,
                expected_suborg_id=suborg,
                side=side,
                maker_amount_ceiling=cap,
                wallet_address=wallet_value,
                builder_code=builder_value,
                verifying_contract=contract,
            )
            return True
        except tk.TurnkeyRefused:
            return False

    checks["entry_allows_buy"] = allowed(base_buy)
    checks["exit_allows_sell"] = allowed(base_sell, side="SELL", cap=sell_cap)
    checks["wrong_tenant_denied"] = not allowed(base_buy, tenant_value="wrong")
    checks["wrong_suborg_denied"] = not allowed(base_buy, suborg_value="wrong")

    mutations = {
        "wrong_wallet_denied": ("message", "maker", "0x" + "9" * 40),
        "wrong_signer_denied": ("message", "signer", "0x" + "8" * 40),
        "wrong_chain_denied": ("domain", "chainId", 1),
        "wrong_domain_version_denied": ("domain", "version", "1"),
        "wrong_contract_denied": ("domain", "verifyingContract", tk.NEG_RISK_CTF_EXCHANGE_V2),
        "wrong_signature_type_denied": ("message", "signatureType", 3),
        "wrong_side_denied": ("message", "side", 1),
        "wrong_builder_denied": ("message", "builder", "0x" + "7" * 64),
        "buy_collateral_cap_plus_one_denied": ("message", "makerAmount", buy_cap + 1),
    }
    for name, (section, field, value) in mutations.items():
        changed = copy.deepcopy(base_buy)
        changed[section][field] = value
        checks[name] = not allowed(changed)
    sell_plus = copy.deepcopy(base_sell)
    sell_plus["message"]["makerAmount"] = sell_cap + 1
    checks["sell_share_cap_plus_one_denied"] = not allowed(
        sell_plus, side="SELL", cap=sell_cap
    )

    batch = copy.deepcopy(base_buy)
    batch["primaryType"] = tk.BATCH_PRIMARY_TYPE
    batch["domain"]["name"] = tk.BATCH_DOMAIN_NAME
    checks["batch_denied"] = not allowed(batch)
    transfer = copy.deepcopy(base_buy)
    transfer["primaryType"] = "TransferWithAuthorization"
    transfer["domain"]["name"] = "USD Coin"
    checks["transfer_withdraw_shape_denied"] = not allowed(transfer)

    signer = tk.TurnkeySigner(
        wallet, organization_id=suborg,
        priv_hex=entry["private_key"], pub_hex=entry["public_key"],
    )
    for method in ("sign_transaction", "sign_message", "unsafe_sign_hash"):
        try:
            getattr(signer, method)(b"not-signed")
            checks[f"{method}_denied"] = False
        except tk.TurnkeyRefused:
            checks[f"{method}_denied"] = True

    raw = {
        "suborg_id": suborg,
        "users": [{
            "userId": "root-user-sensitive-id", "userName": "owner root",
            "authenticators": [{"credentialId": "credential-sensitive"}],
            "apiKeys": [],
        }],
        "authenticators": [{
            "userId": "root-user-sensitive-id",
            "authenticators": [{
                "authenticatorId": "auth-sensitive-id",
                "credentialId": "credential-sensitive",
                "authenticatorName": "device", "transports": ["AUTHENTICATOR_TRANSPORT_INTERNAL"],
            }],
        }],
        "wallets": [], "accounts": [], "policies": [],
        "activities": [{
            "id": "activity-sensitive-id", "type": "ACTIVITY_TYPE_CREATE_USERS_V4",
            "status": "ACTIVITY_STATUS_COMPLETED", "intent": {"challenge": "never-write-me"},
        }],
    }
    redacted = tk.redact_readonly_snapshot(raw)
    encoded = json.dumps(redacted, sort_keys=True)
    checks["reconcile_evidence_is_redacted"] = all(
        secret not in encoded for secret in (
            "root-user-sensitive-id", "credential-sensitive", "auth-sensitive-id",
            "activity-sensitive-id", "never-write-me",
        )
    )
    checks["reconcile_has_digest"] = redacted["snapshot_digest"].startswith("sha256:")
    return checks


if __name__ == "__main__":
    result = selftest()
    print(json.dumps({"PASS": all(result.values()), "total": len(result), "checks": result},
                     indent=2, sort_keys=True))
    raise SystemExit(0 if all(result.values()) else 1)
