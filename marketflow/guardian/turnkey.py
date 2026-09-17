"""Guardian Turnkey signing layer: enclave-held keys + EIP-712 policy scoping.

**Why this exists.** Until now a hosted tenant's private key sat in a Fernet blob
on this box (`wallet.py`), decrypted into process memory to sign. That makes the
signing process a single point of total loss: whoever owns the process owns the
key, and the key can move funds. Three facts, each measured first-hand against
the live venue, forced the design here:

  1. Polymarket's `DepositWallet` has a native `authorizeSessionSigner` mechanism
     — but the official relayer REFUSES to forward that call (measured:
     `call blocked: self-call "authorizeSessionSigner" not allowed`), and the
     contract path is `onlySelf` -> `onlyFactory` -> `onlyOperator`, so there is
     no other route. Zero such authorizations exist on all of Polygon.
  2. Writing our own ERC-1271 account cannot work either: the CLOB's L1 auth does
     a plain ECDSA recover, so a contract address can never obtain L2 credentials
     (a contract WITH `isValidSignature`, one WITHOUT, and a random address all
     return the identical `Invalid L1 Request headers`), and the order endpoint
     requires `order.signer` to be the API key's own address.
  3. The relayer forwards an owner-signed `pUSD.transfer` without complaint
     (measured: accepted and mined). **Nobody upstream stops a stolen owner key
     from draining the wallet.** That defence has to be ours.

So delegation moves DOWN a layer: the signer stays the tenant's own EOA (Polymarket
sees an ordinary wallet, no special support needed), but the key material lives in
Turnkey's enclave and this process holds only a policy-scoped credential that can
sign ONE shape of message.

**The separation that makes it safe** — verified on real payloads 2026-08-01:

    trading  = EIP-712 domain `Polymarket CTF Exchange`, primaryType `TypedDataSign`
    moving   = EIP-712 domain `DepositWallet`,           primaryType `Batch`

Two disjoint shapes. The policy allows only the first.

**What this actually buys, stated honestly** (Key audit, 2026-08-01 — an earlier
version of this docstring claimed "cannot move funds", which is wrong and would
mislead whoever reads it during an incident):

  * A compromised process **cannot transfer** out of the wallet, and cannot raise
    the per-order notional (that lives in the policy; changing it needs Turnkey
    root, not this box).
  * It **can still lose the money by trading it**: the policy has a per-signature
    cap but no cumulative limit, no rate limit and no counterparty constraint, so
    an attacker can wash-trade against their own order at cap-sized losses in a
    loop. `takerAmount` is unconstrained, so each loop can be a total loss of one
    cap.
  * So the real gain is a **time window plus an audit trail**: "instant total
    loss" becomes "drained over tens of minutes, with every signature logged
    enclave-side". That window is what monitoring and a DENY policy can act in.
    Bounding the loss itself takes a hot-balance limit, not a policy.

Production payloads are the ERC-7739 NESTED form (`signatureType=3` /
DEPOSIT_WALLET): the order body sits under `message['contents']`, NOT at the top
level. Policies written against a bare `Order` primaryType would silently match
nothing here.

Deliberately NOT abstracted: this module speaks Turnkey's REST API directly with
stdlib + `cryptography` (already a guardian dependency) rather than pulling in
another SDK — the whole surface is one signing call plus provisioning.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
API_BASE = os.environ.get("MARKETFLOW_TURNKEY_API_BASE", "https://api.turnkey.com")
CONFIG_FILE = os.environ.get(
    "MARKETFLOW_TURNKEY_CONFIG",
    os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "turnkey_api_key.json"),
)
# Default urllib UA is rejected by Turnkey's edge as a bot signature (HTTP 403
# `error code: 1010`) — same trap as the chain RPCs in . Always send one.
USER_AGENT = "marketflow-guardian-turnkey/0.1"
HTTP_TIMEOUT_SEC = 25

# Polymarket EIP-712 identities. TRADING shape (allowed) vs FUND-MOVING shape
# (never allowed) — both measured off the SDK's own builders, not guessed.
CLOB_DOMAIN_NAME = "Polymarket CTF Exchange"
CLOB_DOMAIN_VERSION = "2"
CLOB_ORDER_PRIMARY_TYPE = "TypedDataSign"  # 1271/DEPOSIT_WALLET nested form
CLOB_V2_ORDER_PRIMARY_TYPE = "Order"        # Phase 1 empty-wallet EOA form
CLOB_AUTH_DOMAIN_NAME = "ClobAuthDomain"
CLOB_AUTH_PRIMARY_TYPE = "ClobAuth"
BATCH_DOMAIN_NAME = "DepositWallet"
BATCH_PRIMARY_TYPE = "Batch"
DEPOSIT_WALLET_DOMAIN_NAME = "DepositWallet"
DEPOSIT_WALLET_DOMAIN_VERSION = "1"
POLYGON_CHAIN_ID = 137
CTF_EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310f59"
PHASE1_EXCHANGE_CONTRACT = CTF_EXCHANGE_V2
EOA_SIGNATURE_TYPE = 0
SIDE_CODE = {"BUY": 0, "SELL": 1}
BYTES32_ZERO = "0x" + "0" * 64
# Keep the disposable Phase 1 agent credentials short-lived.  Turnkey validates
# the window when the credential is created; one hour is enough for the live
# empty-wallet matrix and leaves a materially smaller pre-revocation window.
USER_ROOT_AGENT_TTL_SEC = 60 * 60


def api_key_expiration_seconds(
    *, ttl_seconds: int = USER_ROOT_AGENT_TTL_SEC,
) -> str:
    """Return Turnkey V8's relative API-key validity window in seconds.

    ``expirationSeconds`` is a duration from credential creation, not a Unix
    timestamp.  Query responses preserve the submitted duration verbatim.
    """
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise TurnkeyError("ttl_seconds must be a positive integer")
    return str(ttl_seconds)

# Fleet gate, same file-presence pattern as GUARDIAN_LIVE_ENABLED /
# GUARDIAN_ENTRY_ENABLED: absent = no tenant signs through Turnkey, whatever the
# per-tenant field says. Lets an incident be stopped by deleting one file.
TURNKEY_ENABLED_FILENAME = "GUARDIAN_TURNKEY_ENABLED"

KEY_BACKEND_LOCAL = "local"
KEY_BACKEND_TURNKEY = "turnkey"
KEY_BACKEND_TURNKEY_USER_ROOT = "turnkey_user_root"


class TurnkeyError(Exception):
    """Transport / API / configuration failure."""


class TurnkeyRefused(TurnkeyError):
    """A signature this layer must never produce was requested.

    Raised locally, BEFORE the request leaves the box, so a fund-moving payload
    never even reaches the enclave. The policy is the second line, not the only
    one — belt and braces on the one path that loses money.
    """


# --------------------------------------------------------------------------
# policy conditions (verified strings — see the syntax traps below)
# --------------------------------------------------------------------------
#
# Traps measured 2026-08-01, all three cost a round trip to find:
#   * `int(...)` casts do NOT parse (the engine lists its legal operators on error).
#   * Nested access works ONLY subscript-style: message['contents']['makerAmount'].
#     Dotted `message.contents.makerAmount` is rejected.
#   * The amount must be a JSON NUMBER. Serialised as a string, a compliant order
#     is DENIED too (string-vs-number comparison simply fails to match). That is
#     fail-closed — the failure mode is "no trading", never "no limit" — but it
#     would look like an outage, so `assert_amount_is_numeric` guards it and a
#     selftest pins the SDK's int typing.


def _wallet_binding(signer_address: str, private_key_id: str | None = None) -> str:
    """Bind a policy to ONE signing resource.

    Without this a policy is scoped only by payload shape and approver, so any
    agent in the org could sign an order for ANY wallet in the org: tenant A's
    compromised agent could trade tenant B's book. It cannot steal B's funds — the
    order shape is still all that is allowed — but "trade a stranger's account" is
    not a residual risk worth carrying in a multi-tenant custodian.

    Two resource kinds, two different fields, and they are NOT interchangeable:
    Guardian's hosted tenants get generated HD wallets (bound by
    `wallet_account.address`), while an existing EOA brought in from elsewhere is
    an imported private key, which exposes `id`/`tags` but **no address field** —
    measured: a condition on `private_key.address` is rejected outright at policy
    creation with `field does not exist: address`. So the caller says which kind
    it has, and exactly one form is emitted; guessing or OR-ing both would either
    fail to parse or silently evaluate against a field that is not there.
    """
    if private_key_id:
        return f"private_key.id == '{private_key_id}'"
    if not (isinstance(signer_address, str) and signer_address.startswith("0x") and len(signer_address) == 42):
        raise TurnkeyError(f"implausible signer address for policy binding: {signer_address!r}")
    # EXACT checksum form, never lower-cased: the comparison is a case-sensitive
    # string match against the address as Turnkey stores it. A lower-cased binding
    # parses and deploys happily and then denies every signature — measured, and
    # it looks exactly like a broken policy rather than a formatting slip.
    return f"wallet_account.address == '{signer_address}'"


def order_policy_condition(cap_micro_usd: int, signer_address: str,
                           private_key_id: str | None = None) -> str:
    """Allow exactly one shape: a CLOB order on Polygon, from ONE wallet, at or
    under `cap_micro_usd`.

    `cap_micro_usd` is pUSD base units (6 dp), so 25_000_000 == $25. The cap lives
    inside the enclave's rule set: a fully compromised Guardian process cannot
    raise it, unlike the in-process fuse caps it backs up.
    """
    if not isinstance(cap_micro_usd, int) or cap_micro_usd <= 0:
        raise TurnkeyError(f"cap must be a positive int of pUSD base units, got {cap_micro_usd!r}")
    return (
        f"eth.eip_712.primary_type == '{CLOB_ORDER_PRIMARY_TYPE}'"
        f" && eth.eip_712.domain.name == '{CLOB_DOMAIN_NAME}'"
        f" && eth.eip_712.domain.chain_id == {POLYGON_CHAIN_ID}"
        f" && eth.eip_712.message['contents']['makerAmount'] <= {cap_micro_usd}"
        f" && {_wallet_binding(signer_address, private_key_id)}"
    )


def clob_auth_policy_condition(signer_address: str, private_key_id: str | None = None) -> str:
    """Allow deriving L2 API credentials (ClobAuth) for ONE wallet.

    Safe to delegate: the resulting credentials can place and cancel orders, which
    this agent may do anyway, and cannot move funds. Without it a Turnkey-backed
    tenant could never (re-)derive credentials. Still wallet-bound — credentials
    for someone else's address are not this agent's business.
    """
    return (
        f"eth.eip_712.primary_type == '{CLOB_AUTH_PRIMARY_TYPE}'"
        f" && eth.eip_712.domain.name == '{CLOB_AUTH_DOMAIN_NAME}'"
        f" && {_wallet_binding(signer_address, private_key_id)}"
    )


def owner_deposit_order_policy_condition(
    *,
    side: str,
    maker_amount_ceiling: int,
    signer_address: str,
    funder_address: str,
    private_key_id: str,
    verifying_contract: str,
    builder_code: str = BYTES32_ZERO,
) -> str:
    """Exact order-only grant for the owner's imported EOA + Deposit Wallet.

    Unlike the legacy policy this pins the nested ERC-7739 message completely:
    side, Deposit Wallet maker/signer, signature type, builder, both domains,
    chain and exchange contract. The imported key is bound by Turnkey resource
    id because imported private keys do not expose an address policy field.

    ``maker_amount_ceiling`` is pUSD base units for BUY and share base units for
    SELL. Callers must create separate BUY/SELL agents and separate policies.
    ClobAuth is intentionally not included: runtime already has L2 credentials,
    so a compromised agent has no reason to derive another credential set.
    """
    selected = str(side or "").strip().upper()
    if selected not in SIDE_CODE:
        raise TurnkeyError("owner Deposit Wallet policy side must be BUY or SELL")
    if not isinstance(maker_amount_ceiling, int) or maker_amount_ceiling <= 0:
        raise TurnkeyError("maker_amount_ceiling must be a positive integer")
    for value, field in (
        (signer_address, "signer_address"),
        (funder_address, "funder_address"),
        (verifying_contract, "verifying_contract"),
    ):
        if not (isinstance(value, str) and value.startswith("0x") and len(value) == 42):
            raise TurnkeyError(f"implausible {field}")
    if not str(private_key_id or "").strip():
        raise TurnkeyError("private_key_id is required")
    if not (
        isinstance(builder_code, str)
        and builder_code.startswith("0x")
        and len(builder_code) == 66
        and all(c in "0123456789abcdefABCDEF" for c in builder_code[2:])
    ):
        raise TurnkeyError("builder_code must be a 32-byte 0x-prefixed hex string")

    # Polymarket's current Deposit Wallet order builder deliberately puts the
    # smart-wallet address in all three ERC-1271 message slots: outer
    # verifyingContract plus inner maker and signer.  The EOA is still pinned by
    # Turnkey's private-key resource id; expecting the EOA inside the message
    # would deny every valid SDK-built order.
    signer = signer_address.lower()
    maker = funder_address.lower()
    contract = verifying_contract.lower()
    builder = builder_code.lower()
    side_code = SIDE_CODE[selected]
    return (
        "activity.type == 'ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2'"
        " && activity.params.encoding == 'PAYLOAD_ENCODING_EIP712'"
        f" && eth.eip_712.primary_type == '{CLOB_ORDER_PRIMARY_TYPE}'"
        f" && eth.eip_712.domain.name == '{CLOB_DOMAIN_NAME}'"
        f" && eth.eip_712.domain.version == '{CLOB_DOMAIN_VERSION}'"
        f" && eth.eip_712.domain.chain_id == {POLYGON_CHAIN_ID}"
        f" && eth.eip_712.domain.verifying_contract == '{contract}'"
        f" && eth.eip_712.message['name'] == '{DEPOSIT_WALLET_DOMAIN_NAME}'"
        f" && eth.eip_712.message['version'] == '{DEPOSIT_WALLET_DOMAIN_VERSION}'"
        f" && eth.eip_712.message['chainId'] == {POLYGON_CHAIN_ID}"
        f" && eth.eip_712.message['verifyingContract'] == '{maker}'"
        f" && eth.eip_712.message['salt'] == '{BYTES32_ZERO}'"
        f" && eth.eip_712.message['contents']['maker'] == '{maker}'"
        f" && eth.eip_712.message['contents']['signer'] == '{maker}'"
        f" && eth.eip_712.message['contents']['signatureType'] == 3"
        f" && eth.eip_712.message['contents']['side'] == {side_code}"
        f" && eth.eip_712.message['contents']['builder'] == '{builder}'"
        f" && eth.eip_712.message['contents']['makerAmount'] <= {maker_amount_ceiling}"
        f" && {_wallet_binding(signer_address, private_key_id)}"
    )


def user_root_order_policy_condition(
    *,
    side: str,
    maker_amount_ceiling: int,
    wallet_address: str,
    builder_code: str,
    verifying_contract: str = PHASE1_EXCHANGE_CONTRACT,
) -> str:
    """Return the exact Phase 1 EOA/V2 order policy for one delegated agent.

    This is deliberately separate from :func:`order_policy_condition`, which is
    the legacy Deposit Wallet / ERC-7739 policy.  Phase 1 creates an empty
    Turnkey EOA in a new sub-organization and signs only the standard V2
    ``Order`` shape (signature type 0).  Nothing here can be used to move funds:
    raw transactions, hashes, Batch and every unlisted activity remain implicit
    DENY in Turnkey.

    ``makerAmount`` is side-dependent in Polymarket V2: BUY is pUSD collateral
    base units; SELL is share base units.  The caller must therefore provide the
    correct side-specific ceiling and the returned policy never calls it USD.
    """
    selected = str(side or "").strip().upper()
    if selected not in SIDE_CODE:
        raise TurnkeyError("user-root policy side must be BUY or SELL")
    if not isinstance(maker_amount_ceiling, int) or maker_amount_ceiling <= 0:
        raise TurnkeyError("maker_amount_ceiling must be a positive integer")
    for value, field in (
        (wallet_address, "wallet_address"),
        (verifying_contract, "verifying_contract"),
    ):
        if not (isinstance(value, str) and value.startswith("0x") and len(value) == 42):
            raise TurnkeyError(f"implausible {field}")
    if not (
        isinstance(builder_code, str)
        and builder_code.startswith("0x")
        and len(builder_code) == 66
        and all(c in "0123456789abcdefABCDEF" for c in builder_code[2:])
    ):
        raise TurnkeyError("builder_code must be a 32-byte 0x-prefixed hex string")

    # Turnkey normalizes EIP-712 hex fields to lowercase.  The resource binding
    # is separate and retains the exact wallet representation returned by
    # Turnkey, while signed message/domain comparisons use lowercase literals.
    maker = wallet_address.lower()
    contract = verifying_contract.lower()
    builder = builder_code.lower()
    code = SIDE_CODE[selected]
    return (
        "activity.type == 'ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2'"
        " && activity.params.encoding == 'PAYLOAD_ENCODING_EIP712'"
        f" && eth.eip_712.primary_type == '{CLOB_V2_ORDER_PRIMARY_TYPE}'"
        f" && eth.eip_712.domain.name == '{CLOB_DOMAIN_NAME}'"
        f" && eth.eip_712.domain.version == '{CLOB_DOMAIN_VERSION}'"
        f" && eth.eip_712.domain.chain_id == {POLYGON_CHAIN_ID}"
        f" && eth.eip_712.domain.verifying_contract == '{contract}'"
        f" && eth.eip_712.message['maker'] == '{maker}'"
        f" && eth.eip_712.message['signer'] == '{maker}'"
        f" && eth.eip_712.message['signatureType'] == {EOA_SIGNATURE_TYPE}"
        f" && eth.eip_712.message['side'] == {code}"
        f" && eth.eip_712.message['builder'] == '{builder}'"
        f" && eth.eip_712.message['makerAmount'] <= {maker_amount_ceiling}"
        f" && {_wallet_binding(wallet_address)}"
    )


def user_root_policy_intent(
    *,
    tenant_id: str,
    suborg_id: str,
    agent_user_id: str,
    side: str,
    maker_amount_ceiling: int,
    wallet_address: str,
    builder_code: str,
    expires_at: str,
    verifying_contract: str = PHASE1_EXCHANGE_CONTRACT,
) -> dict[str, str]:
    """Build one root-approved agent policy intent without any credential data."""
    selected = str(side).upper()
    if selected not in SIDE_CODE:
        raise TurnkeyError("user-root policy side must be BUY or SELL")
    for value, field in (
        (tenant_id, "tenant_id"),
        (suborg_id, "suborg_id"),
        (agent_user_id, "agent_user_id"),
        (expires_at, "expires_at"),
    ):
        if not str(value or "").strip():
            raise TurnkeyError(f"{field} is required")
    unit = "pUSD collateral base units" if selected == "BUY" else "share base units"
    return {
        "policyName": f"marketflow-{tenant_id}-{selected.lower()}-phase1",
        "effect": "EFFECT_ALLOW",
        "consensus": f"approvers.any(user, user.id == '{agent_user_id}')",
        "condition": user_root_order_policy_condition(
            side=selected,
            maker_amount_ceiling=maker_amount_ceiling,
            wallet_address=wallet_address,
            builder_code=builder_code,
            verifying_contract=verifying_contract,
        ),
        "notes": (
            f"MarketFlow Phase 1 empty-wallet delegation; tenant={tenant_id}; "
            f"suborg={suborg_id}; side={selected}; ceiling_unit={unit}; "
            f"agent_api_key_expires_at={expires_at}; no funds and no order submission."
        ),
    }


def build_phase1_order_typed_data(
    *,
    wallet_address: str,
    side: str,
    maker_amount: int,
    builder_code: str = BYTES32_ZERO,
    verifying_contract: str = PHASE1_EXCHANGE_CONTRACT,
    chain_id: int = POLYGON_CHAIN_ID,
    domain_version: str = CLOB_DOMAIN_VERSION,
    signature_type: int = EOA_SIGNATURE_TYPE,
    token_id: int = 1,
    taker_amount: int = 1,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Build a non-replayable-enough-for-tests V2 order shape; never submits it.

    The returned object contains no signature.  Callers must keep it in memory
    and record only a digest/field summary, never the full request or signature.
    """
    selected = str(side or "").strip().upper()
    if selected not in SIDE_CODE:
        raise TurnkeyError("order side must be BUY or SELL")
    if not isinstance(maker_amount, int) or maker_amount <= 0:
        raise TurnkeyError("maker_amount must be a positive integer")
    # Reuse policy validation so malformed addresses/builder values cannot enter
    # the live matrix through a test fixture.
    user_root_order_policy_condition(
        side=selected,
        maker_amount_ceiling=maker_amount,
        wallet_address=wallet_address,
        builder_code=builder_code,
        verifying_contract=verifying_contract,
    )
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        raise TurnkeyError("chain_id must be an integer")
    if isinstance(signature_type, bool) or not isinstance(signature_type, int):
        raise TurnkeyError("signature_type must be an integer")
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    zero = BYTES32_ZERO
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "primaryType": CLOB_V2_ORDER_PRIMARY_TYPE,
        "domain": {
            "name": CLOB_DOMAIN_NAME,
            "version": str(domain_version),
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "salt": ts,
            "maker": wallet_address,
            "signer": wallet_address,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": int(taker_amount),
            "side": SIDE_CODE[selected],
            "signatureType": signature_type,
            "timestamp": ts,
            "metadata": zero,
            "builder": builder_code,
        },
    }


def assert_phase1_order_grant(
    typed_data: dict[str, Any],
    *,
    requested_tenant_id: str,
    expected_tenant_id: str,
    requested_suborg_id: str,
    expected_suborg_id: str,
    side: str,
    maker_amount_ceiling: int,
    wallet_address: str,
    builder_code: str,
    verifying_contract: str = PHASE1_EXCHANGE_CONTRACT,
) -> None:
    """Local fail-closed mirror of the Phase 1 enclave policy.

    This is defense in depth and a deterministic regression oracle; the Turnkey
    policy remains authoritative.  It intentionally checks tenant/sub-org
    context that is not part of EIP-712 and therefore cannot be expressed in the
    policy language itself.
    """
    if requested_tenant_id != expected_tenant_id:
        raise TurnkeyRefused("Phase 1 order tenant mismatch")
    if requested_suborg_id != expected_suborg_id:
        raise TurnkeyRefused("Phase 1 order sub-org mismatch")
    if not isinstance(typed_data, dict):
        raise TurnkeyRefused("Phase 1 order must be EIP-712 typed data")
    selected = str(side or "").strip().upper()
    expected_condition = user_root_order_policy_condition(
        side=selected,
        maker_amount_ceiling=maker_amount_ceiling,
        wallet_address=wallet_address,
        builder_code=builder_code,
        verifying_contract=verifying_contract,
    )
    if not expected_condition:  # pragma: no cover - validation side effect above
        raise TurnkeyRefused("Phase 1 grant is invalid")
    domain = typed_data.get("domain") or {}
    message = typed_data.get("message") or {}
    checks = (
        (typed_data.get("primaryType") == CLOB_V2_ORDER_PRIMARY_TYPE, "primary type"),
        (domain.get("name") == CLOB_DOMAIN_NAME, "domain name"),
        (str(domain.get("version")) == CLOB_DOMAIN_VERSION, "domain version"),
        (domain.get("chainId") == POLYGON_CHAIN_ID, "chain"),
        (str(domain.get("verifyingContract") or "").lower() == verifying_contract.lower(), "contract"),
        (str(message.get("maker") or "").lower() == wallet_address.lower(), "maker"),
        (str(message.get("signer") or "").lower() == wallet_address.lower(), "signer"),
        (message.get("signatureType") == EOA_SIGNATURE_TYPE, "signature type"),
        (message.get("side") == SIDE_CODE[selected], "side"),
        (str(message.get("builder") or "").lower() == builder_code.lower(), "builder"),
        (
            isinstance(message.get("makerAmount"), int)
            and not isinstance(message.get("makerAmount"), bool)
            and 0 < message["makerAmount"] <= maker_amount_ceiling,
            "makerAmount ceiling",
        ),
    )
    for allowed, field in checks:
        if not allowed:
            raise TurnkeyRefused(f"Phase 1 order {field} mismatch")


def phase1_sign_typed_data(
    *,
    typed_data: dict[str, Any],
    agent_private_key: str,
    agent_public_key: str,
    requested_tenant_id: str,
    expected_tenant_id: str,
    requested_suborg_id: str,
    expected_suborg_id: str,
    side: str,
    maker_amount_ceiling: int,
    wallet_address: str,
    builder_code: str,
    verifying_contract: str = PHASE1_EXCHANGE_CONTRACT,
) -> bytes:
    """Validate then ask Turnkey to sign; returns signature bytes only in memory."""
    assert_phase1_order_grant(
        typed_data,
        requested_tenant_id=requested_tenant_id,
        expected_tenant_id=expected_tenant_id,
        requested_suborg_id=requested_suborg_id,
        expected_suborg_id=expected_suborg_id,
        side=side,
        maker_amount_ceiling=maker_amount_ceiling,
        wallet_address=wallet_address,
        builder_code=builder_code,
        verifying_contract=verifying_contract,
    )
    signer = TurnkeySigner(
        wallet_address,
        organization_id=expected_suborg_id,
        priv_hex=agent_private_key,
        pub_hex=agent_public_key,
    )
    return signer.sign_typed_data(full_message=typed_data).signature


def assert_amount_is_numeric(typed_data: dict[str, Any]) -> None:
    """Fail loudly if a CLOB order carries `makerAmount` as a string.

    A string amount is denied by the cap condition even when the order is well
    within the limit, so every trade would stop with a permissions error that
    reads nothing like its cause. Catch it here where the message is honest.
    """
    if typed_data.get("primaryType") != CLOB_ORDER_PRIMARY_TYPE:
        return
    contents = (typed_data.get("message") or {}).get("contents") or {}
    amount = contents.get("makerAmount")
    if amount is not None and not isinstance(amount, int):
        raise TurnkeyError(
            "order makerAmount must be a JSON number for the enclave notional cap to "
            f"evaluate; got {type(amount).__name__} {amount!r}. A string amount is "
            "DENIED by policy even when under the cap (fail-closed, looks like an outage)."
        )


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def load_admin_config(path: str = CONFIG_FILE) -> dict[str, str]:
    """Read the ROOT credential. Provisioning only — never on the execution path.

    **This file must never reach the VPS.** A Turnkey root key can author a new
    policy allowing anything, so a root credential sitting on the exposed box
    reduces the entire separation to decoration: an attacker would not need to
    defeat the order-only policy, they would simply write themselves a better one.

    Root therefore lives on the owner's machine and is used for provisioning and the
    approval setup step (both human-initiated). The VPS carries only per-tenant
    agent credentials, which can sign orders and nothing else. `runtime_org_id`
    and `build_turnkey_client` are the enforcement: the hot path never calls this.
    """
    if not os.path.exists(path):
        raise TurnkeyError(f"no Turnkey config at {path}")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    missing = [k for k in ("organization_id", "api_public_key", "api_private_key") if not raw.get(k)]
    if missing:
        raise TurnkeyError(f"Turnkey config missing {missing}")
    if str(raw["organization_id"]).startswith("PASTE"):
        raise TurnkeyError("Turnkey config still holds template placeholders")
    return {
        "organization_id": str(raw["organization_id"]).strip(),
        "api_public_key": str(raw["api_public_key"]).strip(),
        "api_private_key": str(raw["api_private_key"]).strip(),
    }


# Kept as the old name for callers that legitimately want the root credential.
load_config = load_admin_config


def runtime_org_id(secrets: dict[str, Any]) -> str:
    """The tenant's org id, read from the tenant's own record.

    Deliberately NOT from the root config file: the execution path must work — and
    must only work — with material that carries no authority beyond signing orders.
    An org id is not a secret; the root private key next to it is.
    """
    org = str(secrets.get("turnkey_organization_id") or "").strip()
    if not org:
        raise TurnkeyError(
            "tenant record has no turnkey_organization_id; refusing to fall back to the "
            "root config (the execution path must never load root credentials)"
        )
    return org


SECRET_FIELDS = frozenset({
    "turnkey_agent_private_key", "turnkey_entry_agent_private_key",
    "turnkey_exit_agent_private_key", "api_private_key", "private_key",
})


def redact_provisioning(material: dict[str, Any]) -> dict[str, Any]:
    """Provisioning output with key material removed. Log THIS, never the original.

    `provision_tenant` returns a freshly generated agent private key. One
    `audit(..., **result)` or `log.info(result)` writes it to disk in the clear,
    and audit logs are exactly the files that get copied around during an
    incident.
    """
    return {k: ("<redacted>" if k in SECRET_FIELDS else v) for k, v in material.items()}


def _private_key(priv_hex: str):
    """Parse a P-256 API private key, WITHOUT putting it in an exception.

    `int(priv_hex, 16)` on a malformed value raises
    `ValueError: invalid literal for int() with base 16: '<the whole key>'`. That
    exception is not caught by the transport layer, and the Polymarket SDK
    re-wraps whatever reaches it as `SigningError(f"Failed to sign order: {error}")`
    — so a single stray `0x` prefix would print the agent key into logs and TG
    alerts. Validate first; on failure say which field, never what was in it.
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    assert_api_private_key(priv_hex)
    return ec.derive_private_key(int(priv_hex, 16), ec.SECP256R1())


def assert_api_private_key(priv_hex: Any, *, field: str = "turnkey agent private key") -> None:
    """Shape check only. The value is never echoed, not even truncated."""
    if not isinstance(priv_hex, str) or not priv_hex:
        raise TurnkeyError(f"{field} is missing or not a string")
    candidate = priv_hex.strip()
    if len(candidate) != 64 or any(c not in "0123456789abcdefABCDEF" for c in candidate):
        raise TurnkeyError(
            f"{field} must be exactly 64 hex characters with no 0x prefix "
            "(value withheld from this message on purpose)"
        )


def stamp(body: str, priv_hex: str, pub_hex: str) -> str:
    """Turnkey's auth: P-256 sign the exact request body, base64url the envelope."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    signature = _private_key(priv_hex).sign(body.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    envelope = json.dumps(
        {
            "publicKey": pub_hex,
            "scheme": "SIGNATURE_SCHEME_TK_API_P256",
            "signature": signature.hex(),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(envelope.encode("utf-8")).decode("ascii").rstrip("=")


def call(path: str, payload: dict[str, Any], *, priv_hex: str, pub_hex: str,
         timeout: int = HTTP_TIMEOUT_SEC) -> dict[str, Any]:
    """POST one Turnkey endpoint. The stamp covers the byte-exact body, so the
    body must be serialised once and reused — re-dumping would break the stamp."""
    body = json.dumps(payload, separators=(",", ":"), default=str)
    try:
        # Stamping INSIDE the try: it parses the private key, and a parse failure
        # outside would escape as a bare ValueError carrying the key (see
        # `_private_key`). Everything that touches key material stays wrapped.
        x_stamp = stamp(body, priv_hex, pub_hex)
    except TurnkeyError:
        raise
    except Exception as error:  # noqa: BLE001 - never let a raw crypto error carry key bytes
        raise TurnkeyError(f"failed to stamp Turnkey request for {path} ({type(error).__name__})") from None
    request = urllib.request.Request(
        API_BASE + path,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Stamp": x_stamp,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"_body": raw[:400]}
        return {"_http_status": error.code, **parsed}
    except Exception as error:  # noqa: BLE001 - network shapes vary; caller fails closed
        raise TurnkeyError(f"Turnkey transport failure on {path}: {error}") from error


_ACTIVITY_ROUTES = {
    "ACTIVITY_TYPE_CREATE_WALLET": "create_wallet",
    "ACTIVITY_TYPE_CREATE_API_ONLY_USERS": "create_api_only_users",
    "ACTIVITY_TYPE_CREATE_POLICY_V3": "create_policy",
    "ACTIVITY_TYPE_CREATE_SUB_ORGANIZATION_V8": "create_sub_organization",
    "ACTIVITY_TYPE_CREATE_USERS_V4": "create_users",
    "ACTIVITY_TYPE_CREATE_API_KEYS_V2": "create_api_keys",
    "ACTIVITY_TYPE_CREATE_POLICIES": "create_policies",
    "ACTIVITY_TYPE_DELETE_API_KEYS": "delete_api_keys",
    "ACTIVITY_TYPE_DELETE_USERS": "delete_users",
    "ACTIVITY_TYPE_DELETE_POLICIES": "delete_policies",
    "ACTIVITY_TYPE_SIGN_TRANSACTION_V2": "sign_transaction",
    "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2": "sign_raw_payload",
}


def submit_activity(kind: str, params: dict[str, Any], *, organization_id: str,
                    priv_hex: str, pub_hex: str) -> dict[str, Any]:
    route = _ACTIVITY_ROUTES.get(kind)
    if route is None:
        raise TurnkeyError(f"unmapped activity type {kind}")
    return call(
        f"/public/v1/submit/{route}",
        {
            "type": kind,
            "timestampMs": str(int(time.time() * 1000)),
            "organizationId": organization_id,
            "parameters": params,
        },
        priv_hex=priv_hex,
        pub_hex=pub_hex,
    )


def _activity_result(response: dict[str, Any], key: str) -> dict[str, Any] | None:
    return ((response.get("activity") or {}).get("result") or {}).get(key)


def whoami(config: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    return call(
        "/public/v1/query/whoami",
        {"organizationId": cfg["organization_id"]},
        priv_hex=cfg["api_private_key"],
        pub_hex=cfg["api_public_key"],
    )


def list_sub_orgs(config: dict[str, str] | None = None) -> dict[str, Any]:
    """Parent-org read-only sub-organization inventory."""
    cfg = config or load_config()
    return call(
        "/public/v1/query/list_suborgs",
        {"organizationId": cfg["organization_id"], "paginationOptions": {"limit": "100"}},
        priv_hex=cfg["api_private_key"],
        pub_hex=cfg["api_public_key"],
    )


def _parent_readonly_query(
    route: str,
    suborg_id: str,
    *,
    config: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query a sub-org with the parent credential; never submits an activity."""
    if not str(suborg_id or "").strip():
        raise TurnkeyError("suborg_id is required for read-only query")
    if not route.startswith("/") or "/query/" not in route:
        raise TurnkeyError("parent post-creation access is query-only")
    cfg = config or load_config()
    payload: dict[str, Any] = {"organizationId": str(suborg_id)}
    if extra:
        payload.update(extra)
    return call(
        route,
        payload,
        priv_hex=cfg["api_private_key"],
        pub_hex=cfg["api_public_key"],
    )


def parent_readonly_snapshot(
    suborg_id: str,
    *,
    config: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read users/authenticators/wallets/policies/activities without mutation.

    The raw result is for in-memory reconciliation only.  It can contain WebAuthn
    credential IDs and historical activity intents, so callers must pass it to
    :func:`redact_readonly_snapshot` before writing or printing anything.
    """
    users = _parent_readonly_query(
        "/public/v1/query/list_users", suborg_id, config=config
    )
    wallets = _parent_readonly_query(
        "/public/v1/query/list_wallets", suborg_id, config=config
    )
    accounts = _parent_readonly_query(
        "/public/v1/query/list_wallet_accounts",
        suborg_id,
        config=config,
        extra={"includeWalletDetails": True, "paginationOptions": {"limit": "100"}},
    )
    policies = _parent_readonly_query(
        "/public/v1/query/list_policies", suborg_id, config=config
    )
    activities = _parent_readonly_query(
        "/public/v1/query/list_activities",
        suborg_id,
        config=config,
        extra={"paginationOptions": {"limit": "100"}},
    )
    authenticator_rows: list[dict[str, Any]] = []
    for user in users.get("users") or []:
        user_id = str((user or {}).get("userId") or "")
        if not user_id:
            continue
        result = _parent_readonly_query(
            "/public/v1/query/get_authenticators",
            suborg_id,
            config=config,
            extra={"userId": user_id},
        )
        authenticator_rows.append(
            {"userId": user_id, "authenticators": result.get("authenticators") or []}
        )
    return {
        "suborg_id": str(suborg_id),
        "users": users.get("users") or [],
        "authenticators": authenticator_rows,
        "wallets": wallets.get("wallets") or [],
        "accounts": accounts.get("accounts") or [],
        "policies": policies.get("policies") or [],
        "activities": activities.get("activities") or [],
    }


def _fingerprint(value: Any) -> str:
    return "sha256:" + __import__("hashlib").sha256(
        str(value or "").encode("utf-8")
    ).hexdigest()[:16]


def redact_readonly_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw Turnkey snapshot to non-replayable authority evidence."""
    users = []
    for user in snapshot.get("users") or []:
        users.append(
            {
                "user_id_digest": _fingerprint(user.get("userId")),
                "name": str(user.get("userName") or ""),
                "api_keys": [
                    {
                        "api_key_id_digest": _fingerprint(key.get("apiKeyId")),
                        "name": str(key.get("apiKeyName") or ""),
                        "public_key_digest": _fingerprint(
                            (key.get("credential") or {}).get("publicKey")
                        ),
                        "expiration_seconds": key.get("expirationSeconds"),
                    }
                    for key in user.get("apiKeys") or []
                ],
                "authenticator_count": len(user.get("authenticators") or []),
            }
        )
    authenticators = []
    for row in snapshot.get("authenticators") or []:
        authenticators.append(
            {
                "user_id_digest": _fingerprint(row.get("userId")),
                "items": [
                    {
                        "authenticator_id_digest": _fingerprint(item.get("authenticatorId")),
                        "credential_id_digest": _fingerprint(item.get("credentialId")),
                        "name": str(item.get("authenticatorName") or ""),
                        "transports": list(item.get("transports") or []),
                    }
                    for item in row.get("authenticators") or []
                ],
            }
        )
    policies = [
        {
            "policy_id_digest": _fingerprint(policy.get("policyId")),
            "name": str(policy.get("policyName") or ""),
            "effect": str(policy.get("effect") or ""),
            "consensus_digest": _fingerprint(policy.get("consensus")),
            "condition_digest": _fingerprint(policy.get("condition")),
            "notes_digest": _fingerprint(policy.get("notes")),
        }
        for policy in snapshot.get("policies") or []
    ]
    activities = [
        {
            "activity_id_digest": _fingerprint(activity.get("id")),
            "type": str(activity.get("type") or ""),
            "status": str(activity.get("status") or ""),
        }
        for activity in snapshot.get("activities") or []
    ]
    wallets = [
        {
            "wallet_id_digest": _fingerprint(wallet.get("walletId")),
            "name": str(wallet.get("walletName") or ""),
            "exported": wallet.get("exported"),
            "imported": wallet.get("imported"),
        }
        for wallet in snapshot.get("wallets") or []
    ]
    accounts = [
        {
            "wallet_id_digest": _fingerprint(account.get("walletId")),
            "address": str(account.get("address") or ""),
            "curve": str(account.get("curve") or ""),
            "path": str(account.get("path") or ""),
            "address_format": str(account.get("addressFormat") or ""),
        }
        for account in snapshot.get("accounts") or []
    ]
    result = {
        "schema": "turnkey-readonly-redacted-v1",
        "suborg_id_digest": _fingerprint(snapshot.get("suborg_id")),
        "users": users,
        "authenticators": authenticators,
        "wallets": wallets,
        "accounts": accounts,
        "policies": policies,
        "activities": activities,
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
    result["snapshot_digest"] = _fingerprint(canonical)
    return result


def create_user_root_suborg(
    *,
    label: str,
    root_user_name: str,
    challenge: str,
    attestation: dict[str, Any],
    config: dict[str, str] | None = None,
    require_parent_empty: bool = True,
) -> dict[str, str]:
    """Parent's *only* Phase 1 mutation: create one empty user-root sub-org.

    The browser-generated passkey attestation is accepted in memory and never
    returned.  After this call completes, all changes inside the sub-org must be
    stamped by the user's passkey; the parent is restricted to query helpers.
    """
    cfg = config or load_config()
    if not all(str(v or "").strip() for v in (label, root_user_name, challenge)):
        raise TurnkeyError("label, root_user_name and challenge are required")
    allowed_attestation = {
        key: attestation.get(key)
        for key in ("credentialId", "clientDataJson", "attestationObject", "transports")
    }
    if not all(allowed_attestation.get(key) for key in (
        "credentialId", "clientDataJson", "attestationObject", "transports"
    )):
        raise TurnkeyError("passkey attestation is incomplete")
    before = list_sub_orgs(cfg)
    current = before.get("organizationIds") or []
    if before.get("_http_status") or not isinstance(current, list):
        raise TurnkeyError("could not prove parent sub-org inventory before creation")
    if require_parent_empty and current:
        raise TurnkeyRefused("parent already has a sub-org; refusing to create a second Phase 1 tenant")
    response = submit_activity(
        "ACTIVITY_TYPE_CREATE_SUB_ORGANIZATION_V8",
        {
            "subOrganizationName": f"marketflow-phase1-{label}",
            "rootUsers": [
                {
                    "userName": root_user_name,
                    "apiKeys": [],
                    "authenticators": [
                        {
                            "authenticatorName": "owner user-root passkey",
                            "challenge": challenge,
                            "attestation": allowed_attestation,
                        }
                    ],
                    "oauthProviders": [],
                }
            ],
            "rootQuorumThreshold": 1,
            "wallet": {
                "walletName": f"marketflow-phase1-empty-{label}",
                "accounts": [
                    {
                        "curve": "CURVE_SECP256K1",
                        "pathFormat": "PATH_FORMAT_BIP32",
                        "path": "m/44'/60'/0'/0/0",
                        "addressFormat": "ADDRESS_FORMAT_ETHEREUM",
                    }
                ],
            },
            "disableEmailRecovery": True,
            "disableEmailAuth": True,
            "disableSmsAuth": True,
            "disableOtpEmailAuth": True,
        },
        organization_id=cfg["organization_id"],
        priv_hex=cfg["api_private_key"],
        pub_hex=cfg["api_public_key"],
    )
    result = _activity_result(response, "createSubOrganizationResultV8") or {}
    suborg_id = str(result.get("subOrganizationId") or "")
    wallet = result.get("wallet") or {}
    addresses = wallet.get("addresses") or []
    root_ids = result.get("rootUserIds") or []
    if not (suborg_id and wallet.get("walletId") and len(addresses) == 1 and len(root_ids) == 1):
        raise TurnkeyError("Turnkey V8 sub-org creation did not return one root and one wallet")
    return {
        "suborg_id": suborg_id,
        "wallet_id": str(wallet["walletId"]),
        "wallet_address": str(addresses[0]),
        "root_user_id": str(root_ids[0]),
    }


def generate_agent_api_key() -> dict[str, str]:
    """Generate a P-256 delegated credential; caller must never log the result."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    ).hex()
    private = f"{key.private_numbers().private_value:064x}"
    return {"private_key": private, "public_key": public}


def user_root_agent_users_parameters(
    *,
    entry_public_key: str,
    exit_public_key: str,
    expiration_seconds: int = USER_ROOT_AGENT_TTL_SEC,
) -> dict[str, Any]:
    """Build the V4 two-agent user intent that the user root must stamp."""
    if not isinstance(expiration_seconds, int) or expiration_seconds <= 0:
        raise TurnkeyError("expiration_seconds must be a positive integer")
    for key, field in (
        (entry_public_key, "entry_public_key"),
        (exit_public_key, "exit_public_key"),
    ):
        if not isinstance(key, str) or len(key) != 66:
            raise TurnkeyError(f"{field} must be a compressed P-256 public key")
    return {
        "users": [
            {
                "userName": "MarketFlow entry-agent BUY",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow entry-agent BUY Phase 1",
                    "publicKey": entry_public_key,
                    "curveType": "API_KEY_CURVE_P256",
                    "expirationSeconds": api_key_expiration_seconds(
                        ttl_seconds=expiration_seconds
                    ),
                }],
                "authenticators": [],
                "oauthProviders": [],
                "userTags": [],
            },
            {
                "userName": "MarketFlow exit-agent SELL",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow exit-agent SELL Phase 1",
                    "publicKey": exit_public_key,
                    "curveType": "API_KEY_CURVE_P256",
                    "expirationSeconds": api_key_expiration_seconds(
                        ttl_seconds=expiration_seconds
                    ),
                }],
                "authenticators": [],
                "oauthProviders": [],
                "userTags": [],
            },
        ]
    }


def user_root_api_only_agent_users_parameters(
    *,
    entry_public_key: str,
    exit_public_key: str,
    expiration_seconds: int | None = None,
) -> dict[str, Any]:
    """Build two root-created API-only agents with unbounded P-256 credentials.

    This is a new user-root request builder.  It intentionally does not call,
    wrap, or share state with the legacy parent-root ``provision_tenant`` path.
    Turnkey's current API still exposes CREATE_API_ONLY_USERS and its narrower
    credential shape avoids the general-user credential validation branch.

    Turnkey V8 rejects an API-only user whose sole credential expires.  The
    credential is therefore unbounded and capability lifetime is enforced by
    the independently expiring, exact BUY/SELL policy.  Final revocation deletes
    the API-only user, because its last API key cannot be deleted in isolation.
    """
    if expiration_seconds is not None:
        raise TurnkeyError(
            "an API-only user's sole credential cannot expire; bound capability by policy"
        )
    for key, field in (
        (entry_public_key, "entry_public_key"),
        (exit_public_key, "exit_public_key"),
    ):
        if not isinstance(key, str) or len(key) != 66:
            raise TurnkeyError(f"{field} must be a compressed P-256 public key")
    return {
        "apiOnlyUsers": [
            {
                "userName": "MarketFlow entry-agent BUY",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow entry-agent BUY Phase 1 active",
                    "publicKey": entry_public_key,
                }],
                "userTags": [],
            },
            {
                "userName": "MarketFlow exit-agent SELL",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow exit-agent SELL Phase 1 active",
                    "publicKey": exit_public_key,
                }],
                "userTags": [],
            },
        ]
    }


def user_root_bootstrap_agent_users_parameters(
    *, entry_public_key: str, exit_public_key: str,
) -> dict[str, Any]:
    """Build the explicitly approved policyless bootstrap agent users.

    Compatibility builder for the policyless create step.  Turnkey V8 requires
    an API-only user's sole credential to be unbounded.  Capability is bounded
    by a one-hour exact policy and final revoke deletes the whole API-only user.
    """
    for key, field in (
        (entry_public_key, "entry_public_key"),
        (exit_public_key, "exit_public_key"),
    ):
        if not isinstance(key, str) or len(key) != 66:
            raise TurnkeyError(f"{field} must be a compressed P-256 public key")
    return {
        "apiOnlyUsers": [
            {
                "userName": "MarketFlow entry-agent BUY",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow entry bootstrap policyless",
                    "publicKey": entry_public_key,
                }],
                "userTags": [],
            },
            {
                "userName": "MarketFlow exit-agent SELL",
                "apiKeys": [{
                    "apiKeyName": "MarketFlow exit bootstrap policyless",
                    "publicKey": exit_public_key,
                }],
                "userTags": [],
            },
        ]
    }


# --------------------------------------------------------------------------
# the signer the Polymarket SDK talks to
# --------------------------------------------------------------------------


class _SignedMessage:
    """Minimal stand-in for eth_account's SignedMessage (the SDK reads .signature)."""

    __slots__ = ("signature",)

    def __init__(self, signature: bytes) -> None:
        self.signature = signature


class TurnkeySigner:
    """A `LocalAccount`-shaped signer whose key lives in Turnkey's enclave.

    The SDK touches exactly three things (counted across the 0.1.0b8 tree):
    `.address` (23 call sites), `.sign_typed_data(full_message=...)` (orders and
    ClobAuth), and `.sign_message(...)` (relayer batch paths only).

    `.sign_message` is REFUSED here. For a DEPOSIT_WALLET tenant that call site is
    the batch-execution path — the only way funds leave the wallet — so a bug or a
    compromised caller reaching for it must abort, not fall through to policy and
    hope. Approvals also live behind it: provisioning a Turnkey tenant therefore
    needs a separate, human-approved setup step (see `provision_tenant` notes).
    """

    __slots__ = ("_address", "_org", "_priv", "_pub", "signed_count")

    def __init__(self, address: str, *, organization_id: str, priv_hex: str, pub_hex: str) -> None:
        if not (isinstance(address, str) and address.startswith("0x") and len(address) == 42):
            raise TurnkeyError(f"implausible signer address {address!r}")
        # Validate the key here too, so a malformed one fails at construction with
        # a message that names the field — not mid-order inside the SDK, where the
        # error text gets re-wrapped and logged.
        assert_api_private_key(priv_hex)
        self._address = address
        self._org = organization_id
        self._priv = priv_hex
        self._pub = pub_hex
        self.signed_count = 0

    @property
    def address(self) -> str:
        return self._address

    def sign_message(self, *_args: Any, **_kwargs: Any):
        raise TurnkeyRefused(
            "sign_message is the relayer batch path (the only way funds leave a "
            "Deposit Wallet); a Turnkey-backed Guardian tenant never signs it. "
            "Order signing goes through sign_typed_data."
        )

    def sign_transaction(self, *_args: Any, **_kwargs: Any):
        raise TurnkeyRefused("raw transaction signing is not delegated to Guardian")

    def unsafe_sign_hash(self, *_args: Any, **_kwargs: Any):
        raise TurnkeyRefused(
            "signing a bare hash would hide the payload from the enclave policy, "
            "defeating the whole separation; refusing"
        )

    def sign_typed_data(self, *, full_message: dict[str, Any], **_kwargs: Any) -> _SignedMessage:
        """Sign EIP-712 typed data inside the enclave, subject to policy.

        The SERIALISED payload is sent (encoding EIP712), never a hash — that is
        what lets the policy read `domain` / `primary_type` / `message`. Signing a
        digest instead would produce the same signature with none of the control.
        """
        if not isinstance(full_message, dict):
            raise TurnkeyError("full_message must be the typed-data dict")
        domain_name = (full_message.get("domain") or {}).get("name")
        if domain_name == BATCH_DOMAIN_NAME or full_message.get("primaryType") == BATCH_PRIMARY_TYPE:
            raise TurnkeyRefused(
                f"refusing to sign a {BATCH_DOMAIN_NAME}/{BATCH_PRIMARY_TYPE} payload: "
                "that is the fund-movement shape, not a trade"
            )
        assert_amount_is_numeric(full_message)

        response = submit_activity(
            "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            {
                "signWith": self._address,
                "payload": json.dumps(full_message, separators=(",", ":"), default=str),
                "encoding": "PAYLOAD_ENCODING_EIP712",
                "hashFunction": "HASH_FUNCTION_NOT_APPLICABLE",
            },
            organization_id=self._org,
            priv_hex=self._priv,
            pub_hex=self._pub,
        )
        result = _activity_result(response, "signRawPayloadResult")
        if not result:
            raise TurnkeyRefused(
                "Turnkey declined to sign "
                f"(primaryType={full_message.get('primaryType')!r}, domain={domain_name!r}): "
                f"{response.get('message') or json.dumps(response)[:240]}"
            )
        signature = assemble_signature(result)
        verify_recovers_to(full_message, signature, self._address)
        self.signed_count += 1
        return _SignedMessage(signature)


class TurnkeyAdminSigner(TurnkeySigner):
    """Setup-only signer that CAN sign batches. Never instantiated by the service.

    A hosted wallet needs two one-time on-chain acts before it can trade — the
    Deposit Wallet deploy and the exchange approvals — and both travel the batch
    path that `TurnkeySigner` refuses. Rather than widen the runtime signer (which
    would hand the daemon the ability to move funds for the sake of a step it runs
    once), setup is a separate, human-initiated operation carrying the ROOT
    credential, which lives on the owner's machine and is never deployed.

    Note what this signer gives up: `sign_message` receives an already-hashed
    EIP-191/712 digest, so the enclave cannot see the payload and no policy can
    inspect it. That is precisely why the runtime path forbids it. The control here
    is not the policy — it is that a human with the off-box root key had to start it.

    `guardian_never_imports_admin_signer` in the selftest keeps this honest.
    """

    def sign_message(self, signable: Any, *_args: Any, **_kwargs: Any) -> _SignedMessage:
        from eth_account.messages import _hash_eip191_message

        digest = _hash_eip191_message(signable)
        response = submit_activity(
            "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            {
                "signWith": self._address,
                "payload": "0x" + digest.hex(),
                "encoding": "PAYLOAD_ENCODING_HEXADECIMAL",
                "hashFunction": "HASH_FUNCTION_NO_OP",
            },
            organization_id=self._org,
            priv_hex=self._priv,
            pub_hex=self._pub,
        )
        result = _activity_result(response, "signRawPayloadResult")
        if not result:
            raise TurnkeyRefused(
                "Turnkey declined the setup signature: "
                f"{response.get('message') or json.dumps(response)[:240]}"
            )
        return _SignedMessage(assemble_signature(result))


def build_admin_client(secrets: dict[str, Any], *, secret_dir: str,
                       builder_api_key: Any | None = None) -> Any:
    """Build a client for the one-time setup acts, using the ROOT credential.

    Separate from `build_turnkey_client` on purpose — importing the wrong one is
    the mistake that would matter, so they do not share a code path and this one
    reads a file the VPS does not have.
    """
    admin = load_admin_config()
    from polymarket import ApiKeyCreds, PRODUCTION, SecureClient

    from marketflow.execution.sdk_proxy import polymarket_sdk_proxy_url, proxied_secure_client_transports

    signer = TurnkeyAdminSigner(
        str(secrets["turnkey_signer_address"]),
        organization_id=admin["organization_id"],
        priv_hex=admin["api_private_key"],
        pub_hex=admin["api_public_key"],
    )
    credentials = ApiKeyCreds(
        key=str(secrets["api_key"]),
        secret=str(secrets["api_secret"]),
        passphrase=str(secrets["passphrase"]),
    )
    with proxied_secure_client_transports(polymarket_sdk_proxy_url(secret_dir)):
        return SecureClient._construct_for_wallet(
            signer=signer,
            wallet=str(secrets["funder_address"]),
            environment=PRODUCTION,
            credentials=credentials,
            api_key=builder_api_key,
            logger=None,
        )


def run_tenant_setup(secrets: dict[str, Any], *, secret_dir: str,
                     builder_api_key: Any | None = None) -> dict[str, Any]:
    """One-time exchange approvals for a Turnkey-backed tenant (human-initiated).

    Idempotent by construction: the SDK resolves which approvals are actually
    missing and submits nothing when the wallet is already set up.
    """
    client = build_admin_client(secrets, secret_dir=secret_dir, builder_api_key=builder_api_key)
    try:
        client.setup_trading_approvals()
        return {"ok": True, "wallet": str(secrets["funder_address"])}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - close is best effort
            pass


def assemble_signature(result: dict[str, Any]) -> bytes:
    """r/s/v -> the 65-byte Ethereum signature.

    Turnkey returns `v` as a bare recovery id ("00"/"01"); Ethereum wants 27/28.
    Getting this wrong yields a signature that signs fine and then fails
    verification at the Exchange, so normalise explicitly rather than by luck.
    """
    try:
        r_hex = str(result["r"]).rjust(64, "0")
        s_hex = str(result["s"]).rjust(64, "0")
        v_int = int(str(result["v"]), 16)
    except (KeyError, ValueError) as error:
        raise TurnkeyError(f"malformed Turnkey signature result: {result!r}") from error
    if v_int < 27:
        v_int += 27
    if v_int not in (27, 28):
        raise TurnkeyError(f"recovery id out of range after normalisation: {v_int}")
    return bytes.fromhex(r_hex + s_hex + f"{v_int:02x}")


def verify_recovers_to(typed_data: dict[str, Any], signature: bytes, expected: str) -> None:
    """Recover the signature locally and insist it is the wallet we meant to sign as.

    Catches r/s/v assembly errors and any wallet mix-up (a hosted stack signing as
    the wrong tenant) before the order is submitted, when the cost is an exception
    instead of a wrong account's money.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    recovered = Account.recover_message(
        encode_typed_data(full_message=typed_data), signature=signature
    )
    if recovered.lower() != expected.lower():
        raise TurnkeyError(
            f"signature recovers to {recovered}, expected {expected}; refusing to use it"
        )


# --------------------------------------------------------------------------
# gates + client construction
# --------------------------------------------------------------------------


def turnkey_enabled() -> bool:
    """Fleet gate for ADMITTING tenants to the Turnkey path.

    **Not an incident stop, and deleting it does NOT roll anyone back.** The other
    guardian gates are fail-safe — remove `GUARDIAN_LIVE_ENABLED` and less money
    moves. This one points the other way: if removing it sent migrated tenants
    back to the on-disk key, then anyone with write access to a runtime directory
    could undo the entire enclave separation with a single `rm`, and the on-disk
    key can transfer funds. So it gates admission only; `tenant_is_turnkey_backed`
    is one-way.

    The real incident stop lives in Turnkey: a root-authored `EFFECT_DENY` on
    `ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2`, which takes effect inside the enclave, is
    reversible (delete the DENY to restore), and cannot be undone from this box.
    """
    from marketflow.guardian import store as gstore

    return os.path.exists(os.path.join(gstore.GUARDIAN_ROOT, TURNKEY_ENABLED_FILENAME))


def tenant_is_turnkey_backed(secrets: dict[str, Any]) -> bool:
    """Whether this tenant's key lives in the enclave. Reads the tenant record ONLY.

    Deliberately independent of the fleet gate: once a tenant is on Turnkey there
    is no supported way back to a local key, and a tenant record is not something
    an attacker edits as easily as touching a file in a runtime directory.
    """
    return str(secrets.get("key_backend") or KEY_BACKEND_LOCAL) in {
        KEY_BACKEND_TURNKEY, KEY_BACKEND_TURNKEY_USER_ROOT,
    }


def tenant_is_user_root(secrets: dict[str, Any]) -> bool:
    return str(secrets.get("key_backend") or "") == KEY_BACKEND_TURNKEY_USER_ROOT


def assert_turnkey_path_available(secrets: dict[str, Any]) -> None:
    """For a Turnkey-backed tenant with the gate shut: stop, never fall back.

    A closed gate means "do not trade this tenant", not "trade it with the old
    key". Falling back would silently re-arm the capability the migration existed
    to remove.
    """
    if tenant_is_turnkey_backed(secrets) and not turnkey_enabled():
        raise TurnkeyError(
            f"tenant is Turnkey-backed but {TURNKEY_ENABLED_FILENAME} is absent; "
            "refusing to trade. This gate never falls back to the local key — "
            "restore the gate file, or stop this tenant."
        )


# Retained for readability at call sites that mean "route through Turnkey now".
def tenant_uses_turnkey(secrets: dict[str, Any]) -> bool:
    return tenant_is_turnkey_backed(secrets)


def build_turnkey_client(
    secrets: dict[str, Any],
    *,
    secret_dir: str,
    side: str | None = None,
) -> Any:
    """Build a Polymarket SecureClient that signs through Turnkey.

    `SecureClient.create` is unusable here: it takes a raw private key and calls
    `Account.from_key`. `_construct_for_wallet` takes the signer object itself, so
    the enclave-backed signer slots in with no patching — and it also skips the
    auto-deploy/approval bootstrap that would need the refused batch path.

    Requires the tenant's L2 credentials and funder address to be on file already
    (provisioning writes both). Transports are created inside the scoped proxy
    context, exactly as `pmx.build_secure_client` does — without it CLOB traffic
    leaves from this box's own IP and is geoblocked.
    """
    required = [
        "api_key", "api_secret", "passphrase", "funder_address", "turnkey_signer_address",
    ]
    if tenant_is_user_root(secrets):
        selected_side = str(side or "").strip().upper()
        if selected_side not in ("BUY", "SELL"):
            raise TurnkeyError("user-root client requires an explicit BUY or SELL side")
        prefix = "turnkey_entry_agent" if selected_side == "BUY" else "turnkey_exit_agent"
        agent_private_field = f"{prefix}_private_key"
        agent_public_field = f"{prefix}_public_key"
        required.extend((agent_private_field, agent_public_field))
    else:
        # Legacy platform-root tenants keep their existing one-agent layout.
        agent_private_field = "turnkey_agent_private_key"
        agent_public_field = "turnkey_agent_public_key"
        required.extend((agent_private_field, agent_public_field))
    for field in required:
        if not secrets.get(field):
            raise TurnkeyError(f"Turnkey-backed tenant is missing {field}; refusing to build a client")

    from polymarket import ApiKeyCreds, PRODUCTION, SecureClient

    from marketflow.execution.sdk_proxy import polymarket_sdk_proxy_url, proxied_secure_client_transports

    signer = TurnkeySigner(
        str(secrets["turnkey_signer_address"]),
        organization_id=runtime_org_id(secrets),
        priv_hex=str(secrets[agent_private_field]),
        pub_hex=str(secrets[agent_public_field]),
    )
    credentials = ApiKeyCreds(
        key=str(secrets["api_key"]),
        secret=str(secrets["api_secret"]),
        passphrase=str(secrets["passphrase"]),
    )
    with proxied_secure_client_transports(polymarket_sdk_proxy_url(secret_dir)):
        return SecureClient._construct_for_wallet(
            signer=signer,
            wallet=str(secrets["funder_address"]),
            environment=PRODUCTION,
            credentials=credentials,
            api_key=None,
            logger=None,
        )


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------


def provision_tenant(
    label: str,
    *,
    cap_micro_usd: int,
    config: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an enclave wallet + a zero-permission agent user + its two policies.

    Returns the material to store in the tenant's encrypted blob. The agent's
    private key is generated HERE and is the only thing this box holds; it can
    sign nothing except what the policies allow.

    NOT done here, by design: the Deposit Wallet deploy and the one-time exchange
    approvals both go through the refused batch path. They need a separate setup
    step with its own narrow authority (a human-approved activity, or a setup
    policy restricted to approval calls) — deliberately left out so provisioning
    can never quietly hand this process the ability to move funds.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    cfg = config or load_config()
    org = cfg["organization_id"]
    root = {"priv_hex": cfg["api_private_key"], "pub_hex": cfg["api_public_key"]}
    stamp_suffix = str(int(time.time()))

    wallet_response = submit_activity(
        "ACTIVITY_TYPE_CREATE_WALLET",
        {
            "walletName": f"marketflow-{label}-{stamp_suffix}",
            "accounts": [
                {
                    "curve": "CURVE_SECP256K1",
                    "pathFormat": "PATH_FORMAT_BIP32",
                    "path": "m/44'/60'/0'/0/0",
                    "addressFormat": "ADDRESS_FORMAT_ETHEREUM",
                }
            ],
        },
        organization_id=org,
        **root,
    )
    wallet_result = _activity_result(wallet_response, "createWalletResult") or {}
    addresses = wallet_result.get("addresses") or []
    if not addresses:
        raise TurnkeyError(f"wallet creation returned no address: {json.dumps(wallet_response)[:300]}")

    agent_private = ec.generate_private_key(ec.SECP256R1())
    agent_pub = agent_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    ).hex()
    agent_priv_hex = f"{agent_private.private_numbers().private_value:064x}"

    user_response = submit_activity(
        "ACTIVITY_TYPE_CREATE_API_ONLY_USERS",
        {
            "apiOnlyUsers": [
                {
                    "userName": f"marketflow-agent-{label}-{stamp_suffix}",
                    "apiKeys": [
                        {
                            "apiKeyName": f"marketflow-agent-{label}",
                            "publicKey": agent_pub,
                            "curveType": "API_KEY_CURVE_P256",
                        }
                    ],
                    "userTags": [],
                }
            ]
        },
        organization_id=org,
        **root,
    )
    user_ids = (_activity_result(user_response, "createApiOnlyUsersResult") or {}).get("userIds") or []
    if not user_ids:
        raise TurnkeyError(f"agent user creation failed: {json.dumps(user_response)[:300]}")
    agent_user_id = user_ids[0]

    consensus = f"approvers.any(user, user.id == '{agent_user_id}')"
    signer_address = addresses[0]
    policy_ids: dict[str, str] = {}
    for name, condition in (
        (f"marketflow-{label}-clob-orders", order_policy_condition(cap_micro_usd, signer_address)),
        (f"marketflow-{label}-clob-auth", clob_auth_policy_condition(signer_address)),
    ):
        policy_response = submit_activity(
            "ACTIVITY_TYPE_CREATE_POLICY_V3",
            {
                "policyName": name,
                "effect": "EFFECT_ALLOW",
                "consensus": consensus,
                "condition": condition,
                "notes": "Guardian: trade-only delegation; fund movement is never allowed.",
            },
            organization_id=org,
            **root,
        )
        policy_id = (_activity_result(policy_response, "createPolicyResult") or {}).get("policyId")
        if not policy_id:
            raise TurnkeyError(f"policy {name} failed: {json.dumps(policy_response)[:300]}")
        policy_ids[name] = policy_id

    return {
        "key_backend": KEY_BACKEND_TURNKEY,
        "turnkey_organization_id": org,
        "turnkey_signer_address": addresses[0],
        "turnkey_wallet_id": wallet_result.get("walletId"),
        "turnkey_agent_user_id": agent_user_id,
        "turnkey_agent_public_key": agent_pub,
        "turnkey_agent_private_key": agent_priv_hex,
        "turnkey_policy_ids": policy_ids,
        "turnkey_cap_micro_usd": cap_micro_usd,
    }


def preflight(config: dict[str, str] | None = None) -> dict[str, Any]:
    """Read-only health probe: are the credentials live and is the gate open?

    Never signs. Safe to call from a status route.
    """
    status: dict[str, Any] = {"gate_open": turnkey_enabled(), "config_present": False, "reachable": False}
    try:
        cfg = config or load_config()
        status["config_present"] = True
        status["organization_id"] = cfg["organization_id"]
    except TurnkeyError as error:
        status["error"] = str(error)
        return status
    identity = whoami(cfg)
    if identity.get("organizationId"):
        status["reachable"] = True
        status["organization_name"] = identity.get("organizationName")
        status["user"] = identity.get("username")
    else:
        status["error"] = identity.get("message") or json.dumps(identity)[:200]
    return status


# --------------------------------------------------------------------------
# selftest (offline: no network, no enclave, no SDK)
# --------------------------------------------------------------------------


def selftest() -> dict[str, bool]:
    """Pin the invariants that make this layer safe, without touching Turnkey."""
    checks: dict[str, bool] = {}

    probe_address = "0x" + "d" * 40
    order_condition = order_policy_condition(25_000_000, probe_address)
    # Every policy must name the wallet it applies to: an unbound policy would let
    # one tenant's agent sign orders for another tenant's book.
    checks["policy_binds_to_one_wallet"] = (
        f"wallet_account.address == '{probe_address}'" in order_condition
        and "private_key" not in order_condition
    )
    # Case matters: Turnkey compares the address as a literal string, so a
    # lower-cased binding deploys fine and then denies everything.
    mixed_case = "0xAbC0000000000000000000000000000000000123"
    checks["binding_preserves_checksum_case"] = (
        f"wallet_account.address == '{mixed_case}'"
        in order_policy_condition(25_000_000, mixed_case)
    )
    # Imported keys bind by id: `private_key.address` does not exist and is
    # rejected at policy creation, so the two kinds must never be conflated.
    imported_condition = order_policy_condition(25_000_000, probe_address, "pk-uuid-1")
    checks["imported_key_binds_by_id"] = (
        "private_key.id == 'pk-uuid-1'" in imported_condition
        and "wallet_account" not in imported_condition
        and "private_key.address" not in imported_condition
    )
    owner_funder = "0x" + "e" * 40
    owner_buy = owner_deposit_order_policy_condition(
        side="BUY",
        maker_amount_ceiling=20_000_000,
        signer_address=probe_address,
        funder_address=owner_funder,
        private_key_id="owner-imported-key-id",
        verifying_contract=CTF_EXCHANGE_V2,
    )
    owner_sell = owner_deposit_order_policy_condition(
        side="SELL",
        maker_amount_ceiling=2_000_000_000,
        signer_address=probe_address,
        funder_address=owner_funder,
        private_key_id="owner-imported-key-id",
        verifying_contract=NEG_RISK_CTF_EXCHANGE_V2,
    )
    checks["owner_policy_pins_imported_deposit_wallet_shape"] = all(
        token in owner_buy
        for token in (
            "private_key.id == 'owner-imported-key-id'",
            f"domain.verifying_contract == '{CTF_EXCHANGE_V2}'",
            f"message['verifyingContract'] == '{owner_funder}'",
            f"message['contents']['maker'] == '{owner_funder}'",
            f"message['contents']['signer'] == '{owner_funder}'",
            "message['contents']['signatureType'] == 3",
            "message['contents']['side'] == 0",
            "message['contents']['makerAmount'] <= 20000000",
        )
    )
    checks["owner_policy_separates_buy_and_sell_agents"] = (
        "message['contents']['side'] == 0" in owner_buy
        and "message['contents']['side'] == 1" in owner_sell
        and "message['contents']['makerAmount'] <= 2000000000" in owner_sell
        and CTF_EXCHANGE_V2 in owner_buy
        and NEG_RISK_CTF_EXCHANGE_V2 in owner_sell
    )
    checks["owner_policy_never_allows_auth_or_funds"] = not any(
        token in owner_buy + owner_sell
        for token in (CLOB_AUTH_PRIMARY_TYPE, "primary_type == 'Batch'", "SIGN_TRANSACTION")
    )
    checks["auth_policy_binds_to_one_wallet"] = probe_address in clob_auth_policy_condition(probe_address)
    for bad in ("", "0xshort", "not-an-address"):
        try:
            order_policy_condition(25_000_000, bad)
            checks["policy_rejects_bad_binding_address"] = False
            break
        except TurnkeyError:
            checks["policy_rejects_bad_binding_address"] = True
    checks["policy_pins_nested_order_shape"] = (
        f"primary_type == '{CLOB_ORDER_PRIMARY_TYPE}'" in order_condition
        and "message['contents']['makerAmount'] <= 25000000" in order_condition
    )
    # Dotted nesting and int() casts are both rejected by the engine; if someone
    # "tidies" the condition into either form, every trade stops.
    checks["policy_avoids_rejected_syntax"] = (
        "int(" not in order_condition and "message.contents" not in order_condition
    )
    checks["policy_pins_chain_and_domain"] = (
        f"chain_id == {POLYGON_CHAIN_ID}" in order_condition
        and f"domain.name == '{CLOB_DOMAIN_NAME}'" in order_condition
    )
    # The fund-moving shape must never appear in an ALLOW condition.
    checks["policy_never_allows_batch_shape"] = (
        BATCH_DOMAIN_NAME not in order_condition
        and BATCH_DOMAIN_NAME not in clob_auth_policy_condition(probe_address)
    )
    for bad_cap in (0, -1, 25_000_000.0, "25000000"):
        try:
            order_policy_condition(bad_cap, probe_address)  # type: ignore[arg-type]
            checks["policy_rejects_bad_cap"] = False
            break
        except TurnkeyError:
            checks["policy_rejects_bad_cap"] = True

    signer = TurnkeySigner("0x" + "a" * 40, organization_id="org", priv_hex="ab" * 32, pub_hex="02")
    for method in ("sign_message", "sign_transaction", "unsafe_sign_hash"):
        try:
            getattr(signer, method)(b"x")
            checks[f"signer_refuses_{method}"] = False
        except TurnkeyRefused:
            checks[f"signer_refuses_{method}"] = True
        except Exception:  # noqa: BLE001
            checks[f"signer_refuses_{method}"] = False

    try:
        signer.sign_typed_data(
            full_message={"primaryType": BATCH_PRIMARY_TYPE, "domain": {"name": BATCH_DOMAIN_NAME}}
        )
        checks["signer_refuses_batch_typed_data"] = False
    except TurnkeyRefused:
        checks["signer_refuses_batch_typed_data"] = True
    except Exception:  # noqa: BLE001
        checks["signer_refuses_batch_typed_data"] = False

    for bad_address in ("", "0xshort", "not-an-address", None):
        try:
            TurnkeySigner(bad_address, organization_id="o", priv_hex="ab" * 32, pub_hex="2")  # type: ignore[arg-type]
            checks["signer_rejects_bad_address"] = False
            break
        except TurnkeyError:
            checks["signer_rejects_bad_address"] = True

    # String amounts are denied by policy even under the cap: catch them locally.
    string_amount = {
        "primaryType": CLOB_ORDER_PRIMARY_TYPE,
        "domain": {"name": CLOB_DOMAIN_NAME},
        "message": {"contents": {"makerAmount": "10000"}},
    }
    try:
        assert_amount_is_numeric(string_amount)
        checks["string_amount_rejected"] = False
    except TurnkeyError:
        checks["string_amount_rejected"] = True
    numeric_amount = json.loads(json.dumps(string_amount))
    numeric_amount["message"]["contents"]["makerAmount"] = 10000
    try:
        assert_amount_is_numeric(numeric_amount)
        checks["numeric_amount_accepted"] = True
    except TurnkeyError:
        checks["numeric_amount_accepted"] = False

    # v normalisation: bare recovery ids must become 27/28, and the byte length
    # must be exactly 65 or downstream verification fails obscurely.
    sig_a = assemble_signature({"r": "ab" * 32, "s": "cd" * 32, "v": "00"})
    sig_b = assemble_signature({"r": "ab" * 32, "s": "cd" * 32, "v": "01"})
    checks["signature_v_normalised"] = (
        len(sig_a) == 65 and sig_a[-1] == 27 and len(sig_b) == 65 and sig_b[-1] == 28
    )
    already = assemble_signature({"r": "ab" * 32, "s": "cd" * 32, "v": "1c"})
    checks["signature_v_idempotent"] = already[-1] == 28
    try:
        assemble_signature({"r": "ab" * 32, "s": "cd" * 32})
        checks["signature_rejects_malformed"] = False
    except TurnkeyError:
        checks["signature_rejects_malformed"] = True

    # Default backend is local: an existing tenant is never silently re-routed.
    checks["default_backend_is_local"] = not tenant_is_turnkey_backed({})
    checks["turnkey_needs_explicit_backend"] = not tenant_is_turnkey_backed({"key_backend": "local"})
    # One-way: the gate cannot send a migrated tenant back to its local key.
    checks["turnkey_backing_is_gate_independent"] = tenant_is_turnkey_backed(
        {"key_backend": KEY_BACKEND_TURNKEY})
    checks["user_root_is_turnkey_backed"] = (
        tenant_is_turnkey_backed({"key_backend": KEY_BACKEND_TURNKEY_USER_ROOT})
        and tenant_is_user_root({"key_backend": KEY_BACKEND_TURNKEY_USER_ROOT})
    )
    try:
        assert_turnkey_path_available({"key_backend": KEY_BACKEND_TURNKEY})
        checks["closed_gate_refuses_instead_of_falling_back"] = turnkey_enabled()
    except TurnkeyError:
        checks["closed_gate_refuses_instead_of_falling_back"] = not turnkey_enabled()
    try:
        assert_turnkey_path_available({})
        checks["local_tenant_unaffected_by_turnkey_gate"] = True
    except TurnkeyError:
        checks["local_tenant_unaffected_by_turnkey_gate"] = False

    # --- S5: key material must never reach an exception message ---
    for bad_key in ("0x" + "a" * 64, "zz" * 32, "abc", "", None, 12345):
        try:
            assert_api_private_key(bad_key)
            checks["rejects_malformed_private_key"] = False
            break
        except TurnkeyError as exc:
            if str(bad_key) and str(bad_key) in str(exc):
                checks["rejects_malformed_private_key"] = False
                break
            checks["rejects_malformed_private_key"] = True
    try:
        assert_api_private_key("ab" * 32)
        checks["accepts_wellformed_private_key"] = True
    except TurnkeyError:
        checks["accepts_wellformed_private_key"] = False
    redacted = redact_provisioning({"turnkey_agent_private_key": "ab" * 32, "turnkey_wallet_id": "w1"})
    checks["provisioning_output_redacts_key"] = (
        redacted["turnkey_agent_private_key"] == "<redacted>" and redacted["turnkey_wallet_id"] == "w1")

    # --- credential separation: the hot path must never touch root material ---
    # A tenant record without its own org id must fail rather than quietly borrow
    # the root config; that fallback would give the execution path full authority.
    try:
        runtime_org_id({})
        checks["runtime_refuses_root_org_fallback"] = False
    except TurnkeyError:
        checks["runtime_refuses_root_org_fallback"] = True
    checks["runtime_org_from_tenant_record"] = runtime_org_id({"turnkey_organization_id": "org-x"}) == "org-x"

    # Agent material is mandatory: a tenant missing it must be refused, not
    # silently upgraded to root credentials.
    complete = {
        "api_key": "k", "api_secret": "s", "passphrase": "p",
        "funder_address": "0x" + "b" * 40, "turnkey_signer_address": "0x" + "c" * 40,
        "turnkey_organization_id": "org-x",
        "turnkey_agent_private_key": "ab" * 32, "turnkey_agent_public_key": "02",
    }
    for omitted in ("turnkey_agent_private_key", "turnkey_agent_public_key"):
        partial = {k: v for k, v in complete.items() if k != omitted}
        try:
            build_turnkey_client(partial, secret_dir=HERE)
            checks[f"client_requires_{omitted}"] = False
        except TurnkeyError:
            checks[f"client_requires_{omitted}"] = True
        except Exception:  # noqa: BLE001 - any other failure means the guard did not fire first
            checks[f"client_requires_{omitted}"] = False

    user_root_complete = {
        **{k: v for k, v in complete.items() if not k.startswith("turnkey_agent_")},
        "key_backend": KEY_BACKEND_TURNKEY_USER_ROOT,
        "turnkey_entry_agent_private_key": "ab" * 32,
        "turnkey_entry_agent_public_key": "02",
        "turnkey_exit_agent_private_key": "cd" * 32,
        "turnkey_exit_agent_public_key": "03",
    }
    try:
        build_turnkey_client(user_root_complete, secret_dir=HERE)
        checks["user_root_requires_explicit_side"] = False
    except TurnkeyError:
        checks["user_root_requires_explicit_side"] = True
    for side, omitted in (
        ("BUY", "turnkey_entry_agent_private_key"),
        ("SELL", "turnkey_exit_agent_private_key"),
    ):
        partial = {k: v for k, v in user_root_complete.items() if k != omitted}
        try:
            build_turnkey_client(partial, secret_dir=HERE, side=side)
            checks[f"user_root_{side.lower()}_requires_own_agent"] = False
        except TurnkeyError:
            checks[f"user_root_{side.lower()}_requires_own_agent"] = True
        except Exception:
            checks[f"user_root_{side.lower()}_requires_own_agent"] = False

    # The root config loader must not be reachable from the hot path. Read the
    # body of build_turnkey_client itself and assert no root credential enters it
    # — a real assertion that fails if someone re-adds the fallback.
    import inspect

    hot_path_body = inspect.getsource(build_turnkey_client)
    checks["runtime_path_never_loads_admin_config"] = not any(
        token in hot_path_body for token in ("load_admin_config", "load_config", "api_private_key")
    )
    # The setup-only signer must stay out of the service's import graph.
    for module in ("executor.py", "service.py"):
        module_path = os.path.join(HERE, module)
        if os.path.exists(module_path):
            body = open(module_path, encoding="utf-8").read()
            checks[f"{module.split('.')[0]}_never_uses_admin_signer"] = (
                "TurnkeyAdminSigner" not in body and "build_admin_client" not in body
            )
    # Admin signer really is the batch-capable one (so the split is not cosmetic).
    checks["admin_signer_overrides_sign_message"] = (
        TurnkeyAdminSigner.sign_message is not TurnkeySigner.sign_message
    )
    checks["admin_signer_still_refuses_batch_typed_data"] = "sign_typed_data" not in {
        name for name in TurnkeyAdminSigner.__dict__
    }

    try:
        load_config(path=os.path.join(HERE, "does-not-exist.json"))
        checks["config_missing_fails_closed"] = False
    except TurnkeyError:
        checks["config_missing_fails_closed"] = True

    return checks


if __name__ == "__main__":
    report = selftest()
    print(json.dumps({"PASS": all(report.values()), "checks": report}, indent=2, sort_keys=True))
    sys.exit(0 if all(report.values()) else 1)
