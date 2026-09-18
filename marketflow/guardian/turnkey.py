"""Guardian enclave signing: delegated order agents under the account holder's root.

A delegated mandate never gives this service a key that can move funds. The
account holder owns a Turnkey sub-organization and its root quorum; the trading
key lives in that sub-organization's enclave; this process holds two API
credentials for agents the root created -- an entry agent that may sign BUY
orders and an exit agent that may sign SELL orders -- and nothing else.
Provisioning (the sub-organization, the root quorum, the agents and their
policies) is done with the account holder's root credential, outside this
service. `marketflow/guardian/authority.py` re-validates a read-only proof of
that arrangement before every arm and every signature.

**Why delegation sits at the key layer.** The venue's smart wallet does not let a
third party register a session signer, and CLOB authentication recovers a plain
ECDSA signer, so a contract account cannot hold trading credentials either. The
signer therefore stays an ordinary EOA; only the custody of its key changes. And
because the venue treats an owner-signed transfer as an ordinary wallet
operation, nothing upstream distinguishes a stolen owner key from its owner --
that defence has to live here and in the enclave policy.

**The separation**, as two disjoint EIP-712 shapes:

    trading  = domain `Polymarket CTF Exchange`, primaryType `TypedDataSign`
    moving   = domain `DepositWallet`,           primaryType `Batch`

The agents' policies allow only the first; `TurnkeySigner` refuses the second
locally, before a request leaves the box.

**What this buys, stated honestly:**

  * A compromised process **cannot transfer** out of the wallet and cannot widen
    an agent's policy: both need the account holder's root quorum, which this
    service is never part of.
  * It **can still lose money by trading**: the enclave policy bounds each
    signature, not the running total. The cumulative limits in
    `marketflow/guardian/risk_budget.py` run in this process, so a compromised
    process can ignore them and trade the book badly, one bounded order at a time.
  * So the gain is: no transfer path, a bounded notional per signature, an
    enclave-side record of every signature, and a stop the account holder
    controls -- a root-authored `EFFECT_DENY` that takes effect inside the enclave
    and cannot be lifted from here. Bounding total loss takes a bounded balance in
    the trading wallet, not a policy.

Orders use the ERC-7739 nested form (`signatureType=3`, DEPOSIT_WALLET): the order
body sits under `message['contents']`, not at the top level of the message.

Deliberately not abstracted: this module speaks Turnkey's REST API directly with
the standard library plus `cryptography` rather than another SDK. At run time the
whole surface is one signing call.
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
API_BASE = os.environ.get("MARKETFLOW_TURNKEY_API_BASE", "https://api.turnkey.com")
# The default urllib User-Agent is rejected by Turnkey's edge as a bot signature
# (HTTP 403, `error code: 1010`). Always send one.
USER_AGENT = "marketflow-guardian-turnkey/0.2"
HTTP_TIMEOUT_SEC = 25

# The two EIP-712 identities: the trading shape an agent may sign and the
# fund-moving shape nothing here ever signs.
CLOB_DOMAIN_NAME = "Polymarket CTF Exchange"
CLOB_ORDER_PRIMARY_TYPE = "TypedDataSign"  # ERC-7739 nested order (signatureType 3)
BATCH_DOMAIN_NAME = "DepositWallet"
BATCH_PRIMARY_TYPE = "Batch"

# Fleet gate, same file-presence pattern as GUARDIAN_LIVE_ENABLED /
# GUARDIAN_ENTRY_ENABLED: absent = no tenant signs, whatever its record says.
TURNKEY_ENABLED_FILENAME = "GUARDIAN_TURNKEY_ENABLED"
# The only signing backend a tenant record can name.
KEY_BACKEND_TURNKEY_USER_ROOT = "turnkey_user_root"


class TurnkeyError(Exception):
    """Transport / API / configuration failure."""


class TurnkeyRefused(TurnkeyError):
    """A signature this layer must never produce was requested.

    Raised locally, BEFORE the request leaves the box, so a fund-moving payload
    never even reaches the enclave. The policy is the second line, not the only
    one -- belt and braces on the one path that loses money.
    """


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


def runtime_org_id(secrets: dict[str, Any]) -> str:
    """The tenant's sub-organization id, read from the tenant's own record.

    There is no other source to fall back to: this service holds no organization
    configuration of its own, and signing must work -- and must only work -- with
    material that carries no authority beyond the agents' orders.
    """
    org = str(secrets.get("turnkey_organization_id") or "").strip()
    if not org:
        raise TurnkeyError(
            "tenant record has no turnkey_organization_id; refusing to sign"
        )
    return org


def _private_key(priv_hex: str):
    """Parse a P-256 API private key, WITHOUT putting it in an exception.

    `int(priv_hex, 16)` on a malformed value raises
    `ValueError: invalid literal for int() with base 16: '<the whole key>'`. That
    exception is not caught by the transport layer, and the Polymarket SDK
    re-wraps whatever reaches it as `SigningError(f"Failed to sign order: {error}")`
    — so a single stray `0x` prefix would print the agent key into logs and
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


# The one activity the runtime submits. Anything else is unmapped and refused
# before a request is built.
_ACTIVITY_ROUTES = {
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
    hope. The wallet's one-time deploy and exchange approvals travel the same
    path, which is why they are the account holder's to perform with root, and why
    the authority proof must show both done before this signer is ever built.
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
            "Deposit Wallet); a delegated agent never signs it. "
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

    Catches r/s/v assembly errors and any wallet mix-up (a stack signing as the
    wrong tenant) before the order is submitted, when the cost is an exception
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
    """Fleet gate: absent = no tenant signs through the enclave from this box.

    There is no other signing backend to fall back to, so deleting the file is a
    complete stop here. It is not the stop that binds a compromised box -- that
    one is a root-authored `EFFECT_DENY` on `ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2` in
    the account holder's sub-organization, which takes effect inside the enclave
    and cannot be lifted from this service.
    """
    from marketflow.guardian import store as gstore

    return os.path.exists(os.path.join(gstore.GUARDIAN_ROOT, TURNKEY_ENABLED_FILENAME))


def tenant_is_user_root(secrets: dict[str, Any]) -> bool:
    """Whether this tenant record names the user-root delegation backend.

    Reads the tenant record only. Any other value -- missing, empty, or a name no
    longer supported -- is not a signing backend, and every caller refuses it.
    """
    return str(secrets.get("key_backend") or "") == KEY_BACKEND_TURNKEY_USER_ROOT


def assert_turnkey_path_available(secrets: dict[str, Any]) -> None:
    """Refuse unless the tenant is a user-root delegation and the fleet gate is open."""
    if not tenant_is_user_root(secrets):
        raise TurnkeyError(
            "tenant is not a user-root delegation; delegated mandates sign only "
            "through an agent in the account holder's own sub-organization"
        )
    if not turnkey_enabled():
        raise TurnkeyError(
            f"{TURNKEY_ENABLED_FILENAME} is absent; refusing to sign. There is no "
            "other signing path: restore the gate file, or stop this tenant."
        )


def build_turnkey_client(
    secrets: dict[str, Any],
    *,
    secret_dir: str,
    side: str | None = None,
) -> Any:
    """Build a Polymarket SecureClient whose signatures come from the enclave.

    `SecureClient.create` is unusable here: it takes a raw private key and calls
    `Account.from_key`. `_construct_for_wallet` takes the signer object itself, so
    the enclave-backed signer slots in with no patching -- and it also skips the
    auto-deploy/approval bootstrap that would need the refused batch path.

    The side picks the agent: BUY signs with the entry agent, SELL with the exit
    agent, and each agent's policy admits only its own side. Requires the
    tenant's L2 credentials and funder address on file. Transports are created
    inside the configured proxy context, exactly as `orders.build_secure_client`
    does, so venue traffic leaves through the configured egress rather than this
    host's own address.
    """
    if not tenant_is_user_root(secrets):
        raise TurnkeyError(
            "only a user-root delegation can sign through the enclave; refusing to build a client"
        )
    selected_side = str(side or "").strip().upper()
    if selected_side not in ("BUY", "SELL"):
        raise TurnkeyError("user-root client requires an explicit BUY or SELL side")
    prefix = "turnkey_entry_agent" if selected_side == "BUY" else "turnkey_exit_agent"
    agent_private_field = f"{prefix}_private_key"
    agent_public_field = f"{prefix}_public_key"
    required = [
        "api_key", "api_secret", "passphrase", "funder_address", "turnkey_signer_address",
        agent_private_field, agent_public_field,
    ]
    for field in required:
        if not secrets.get(field):
            raise TurnkeyError(f"user-root tenant is missing {field}; refusing to build a client")

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
# selftest (offline: no network, no enclave, no SDK)
# --------------------------------------------------------------------------


def selftest() -> dict[str, bool]:
    """Pin the invariants that make this layer safe, without touching Turnkey."""
    checks: dict[str, bool] = {}

    signer = TurnkeySigner("0x" + "a" * 40, organization_id="org", priv_hex="ab" * 32, pub_hex="02")
    for method in ("sign_message", "sign_transaction", "unsafe_sign_hash"):
        try:
            getattr(signer, method)(b"x")
            checks[f"signer_refuses_{method}"] = False
        except TurnkeyRefused:
            checks[f"signer_refuses_{method}"] = True
        except Exception:  # noqa: BLE001
            checks[f"signer_refuses_{method}"] = False

    # The fund-moving shape is refused locally, before any request is built.
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

    # String amounts are denied by policy even under the ceiling: catch them locally.
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

    # Only the user-root backend signs. A record naming anything else -- nothing,
    # or a backend name no longer supported -- is refused, never routed elsewhere.
    checks["only_user_root_backend_is_signable"] = (
        tenant_is_user_root({"key_backend": KEY_BACKEND_TURNKEY_USER_ROOT})
        and not any(
            tenant_is_user_root({"key_backend": value} if value is not None else {})
            for value in (None, "", "local", "turnkey", "TURNKEY_USER_ROOT")
        )
    )
    refused_everywhere = True
    for record in ({}, {"key_backend": "local"}, {"key_backend": "turnkey"}):
        try:
            assert_turnkey_path_available(record)
            refused_everywhere = False
        except TurnkeyError:
            pass
    checks["non_user_root_record_refused_by_gate"] = refused_everywhere
    try:
        assert_turnkey_path_available({"key_backend": KEY_BACKEND_TURNKEY_USER_ROOT})
        checks["closed_gate_refuses"] = turnkey_enabled()
    except TurnkeyError:
        checks["closed_gate_refuses"] = not turnkey_enabled()

    # Key material must never reach an exception message.
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

    # A tenant record without its own sub-organization id is refused: there is
    # no organization configuration here to borrow one from.
    try:
        runtime_org_id({})
        checks["runtime_refuses_missing_org"] = False
    except TurnkeyError:
        checks["runtime_refuses_missing_org"] = True
    checks["runtime_org_from_tenant_record"] = runtime_org_id({"turnkey_organization_id": "org-x"}) == "org-x"

    # Client construction: user-root only, explicit side, and each side needs its
    # own agent. Every refusal must fire before the SDK is imported.
    user_root_complete = {
        "key_backend": KEY_BACKEND_TURNKEY_USER_ROOT,
        "api_key": "k", "api_secret": "s", "passphrase": "p",
        "funder_address": "0x" + "b" * 40, "turnkey_signer_address": "0x" + "c" * 40,
        "turnkey_organization_id": "org-x",
        "turnkey_entry_agent_private_key": "ab" * 32,
        "turnkey_entry_agent_public_key": "02",
        "turnkey_exit_agent_private_key": "cd" * 32,
        "turnkey_exit_agent_public_key": "03",
    }
    for label, record, side in (
        ("client_refuses_non_user_root_record",
         {**user_root_complete, "key_backend": "turnkey"}, "BUY"),
        ("client_requires_explicit_side", user_root_complete, None),
    ):
        try:
            build_turnkey_client(record, secret_dir=HERE, side=side)
            checks[label] = False
        except TurnkeyError:
            checks[label] = True
        except Exception:  # noqa: BLE001 - any other failure means the guard did not fire first
            checks[label] = False
    for side, omitted in (
        ("BUY", "turnkey_entry_agent_private_key"),
        ("SELL", "turnkey_exit_agent_private_key"),
    ):
        partial = {k: v for k, v in user_root_complete.items() if k != omitted}
        try:
            build_turnkey_client(partial, secret_dir=HERE, side=side)
            checks[f"client_{side.lower()}_requires_own_agent"] = False
        except TurnkeyError:
            checks[f"client_{side.lower()}_requires_own_agent"] = True
        except Exception:  # noqa: BLE001
            checks[f"client_{side.lower()}_requires_own_agent"] = False

    # The runtime submits exactly one activity type; anything else is refused
    # before a request is built.
    try:
        submit_activity("ACTIVITY_TYPE_SIGN_TRANSACTION_V2", {}, organization_id="o",
                        priv_hex="ab" * 32, pub_hex="02")
        checks["runtime_submits_only_sign_raw_payload"] = False
    except TurnkeyError:
        checks["runtime_submits_only_sign_raw_payload"] = set(_ACTIVITY_ROUTES) == {
            "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2"
        }

    return checks


if __name__ == "__main__":
    report = selftest()
    print(json.dumps({"PASS": all(report.values()), "checks": report}, indent=2, sort_keys=True))
    sys.exit(0 if all(report.values()) else 1)
