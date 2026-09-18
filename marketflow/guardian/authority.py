"""Guardian authority proof and fail-closed user-root gate.

This module does not provision Turnkey users and never signs.  It verifies the
read-only authority snapshot that a separate root-side provisioning/reconcile
process produced.  The hot execution path may proceed only when the snapshot
proves all of the following at once:

* one tenant owns one isolated Turnkey sub-organization;
* the tenant user, not MarketFlow, owns the root quorum;
* MarketFlow holds only a side-specific delegated order agent;
* order shape, wallet, side, side-specific maker amount, builder and expiry are bound; and
* transfer / Batch / withdrawal / export / policy administration are denied.

The user-root route is admission-gated by ``GUARDIAN_USER_ROOT_ENABLED``, and it
is the only route: there is no platform-held root or local key for a mandate to
fall back to.  The proof itself is produced outside this service, by a read-only
reconcile of the account holder's sub-organization; a proof older than
``DEFAULT_MAX_PROOF_AGE_SEC`` refuses every signature until it is refreshed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from marketflow.guardian import store as gstore


AUTHORITY_SCHEMA = "guardian-authority-v0.2"
# Minimum approvals required to act as the mandate's root. A root that one device
# can exercise alone means one compromised device is total loss, which is not an
# acceptable posture for a delegated book. Raise it per deployment; refusing to go
# below two is deliberate and is enforced, not advisory.
MIN_ROOT_QUORUM_THRESHOLD = max(2, int(os.environ.get("MARKETFLOW_MIN_ROOT_QUORUM") or 2))
MODE_TURNKEY_USER_ROOT = "turnkey_user_root"

USER_ROOT_ENABLED_FILENAME = "GUARDIAN_USER_ROOT_ENABLED"
AUTHORITY_FILENAME = "authority.json"
DEFAULT_MAX_PROOF_AGE_SEC = 120.0

CLOB_DOMAIN_NAME = "Polymarket CTF Exchange"
CLOB_DOMAIN_VERSION = "2"
CLOB_PRIMARY_TYPE = "TypedDataSign"
POLYGON_CHAIN_ID = 137
DEPOSIT_WALLET_SIGNATURE_TYPE = 3
EOA_SIGNATURE_TYPE = 0
BYTES32_ZERO = "0x" + "0" * 64

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SIDES = frozenset({SIDE_BUY, SIDE_SELL})

ALLOWED_AGENT_CAPABILITIES = frozenset({"sign_order", "derive_clob_credentials"})
DENIED_POLICY_FLAGS = (
    "allows_batch",
    "allows_transfer",
    "allows_withdraw",
    "allows_key_export",
    "allows_policy_management",
    "allows_raw_transaction",
    "allows_raw_hash",
)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SECRET_KEY_RE = re.compile(
    r"(^|_)(private_?key|secret|passphrase|mnemonic|seed|root_?credential|root_?key)(_|$)",
    re.IGNORECASE,
)


class AuthorityError(ValueError):
    """Authority proof is absent, stale, contradictory, or unsafe."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise AuthorityError(f"authority proof requires {field}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityError(f"authority proof has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise AuthorityError(f"authority proof {field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _now_dt(now: datetime | float | None) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise AuthorityError("now must include timezone")
        return now.astimezone(timezone.utc)
    if now is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(now), tz=timezone.utc)


def _address(value: Any, field: str) -> str:
    address = str(value or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise AuthorityError(f"authority proof has invalid {field}")
    return address


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AuthorityError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AuthorityError(f"{field} must be a positive integer") from exc
    if parsed <= 0 or str(value).strip() != str(parsed):
        raise AuthorityError(f"{field} must be a positive integer")
    return parsed


def _walk_for_secrets(value: Any, path: str = "authority") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _SECRET_KEY_RE.search(key) and "public_key" not in key.lower():
                raise AuthorityError(f"secret/root material is forbidden in authority proof: {path}.{key}")
            _walk_for_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_for_secrets(child, f"{path}[{index}]")


def authority_path(tenant_id: str) -> str:
    return os.path.join(gstore.tenant_dir(str(tenant_id)), AUTHORITY_FILENAME)


def user_root_enabled() -> bool:
    return os.path.exists(
        os.path.join(gstore.GUARDIAN_ROOT, USER_ROOT_ENABLED_FILENAME)
    )


def load_authority(tenant_id: str) -> dict[str, Any]:
    path = authority_path(tenant_id)
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise AuthorityError(f"user-root authority proof missing for {tenant_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"user-root authority proof unreadable for {tenant_id}") from exc
    if not isinstance(document, dict):
        raise AuthorityError("user-root authority proof must be an object")
    return document


def _atomic_write(path: str, document: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def proof_digest(record: Mapping[str, Any]) -> str:
    body = dict(record)
    verification = dict(body.get("verification") or {})
    verification.pop("proof_digest", None)
    body["verification"] = verification
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_user_root_authority(
    record: Mapping[str, Any],
    *,
    expected_tenant_id: str,
    side: str | None = None,
    expected_signer_address: str | None = None,
    expected_funder_address: str | None = None,
    maker_amount_base_units: int | None = None,
    expected_builder_code: str | None = None,
    now: datetime | float | None = None,
    max_proof_age_sec: float = DEFAULT_MAX_PROOF_AGE_SEC,
    require_gate: bool = True,
    require_fresh: bool = True,
) -> dict[str, Any]:
    """Validate a user-root proof and return a public, execution-safe summary.

    This is structural verification of a fresh Turnkey read-only snapshot.  It
    does not replace the enclave policy: both must independently allow a real
    signature.  Every uncertainty raises ``AuthorityError``; callers must never
    catch it by selecting another key backend.
    """
    if not isinstance(record, Mapping):
        raise AuthorityError("authority proof must be an object")
    _walk_for_secrets(record)
    if record.get("schema") != AUTHORITY_SCHEMA:
        raise AuthorityError("unsupported authority proof schema")
    if str(record.get("mode") or "") != MODE_TURNKEY_USER_ROOT:
        raise AuthorityError("authority proof is not turnkey_user_root")
    if require_gate and not user_root_enabled():
        raise AuthorityError(
            f"{USER_ROOT_ENABLED_FILENAME} absent; user-root execution refused"
        )
    tenant_id = str(record.get("tenant_id") or "")
    if tenant_id != str(expected_tenant_id):
        raise AuthorityError("authority proof tenant mismatch")
    if record.get("revoked_at") not in (None, ""):
        raise AuthorityError("authority has been revoked")

    root = record.get("root")
    if not isinstance(root, Mapping):
        raise AuthorityError("authority proof requires root metadata")
    if root.get("owner") != "user" or root.get("marketflow_member") is not False:
        raise AuthorityError("MarketFlow must not be a user-root quorum member")
    root_ids = root.get("user_ids")
    if not isinstance(root_ids, list) or not root_ids or any(not str(v).strip() for v in root_ids):
        raise AuthorityError("authority proof requires user-owned root ids")
    threshold = _positive_int(root.get("quorum_threshold"), "root.quorum_threshold")
    if threshold > len(root_ids):
        raise AuthorityError("root quorum threshold exceeds user root count")
    if threshold < MIN_ROOT_QUORUM_THRESHOLD:
        raise AuthorityError(
            f"root quorum threshold {threshold} is below the required minimum "
            f"{MIN_ROOT_QUORUM_THRESHOLD}"
        )

    suborg = record.get("suborg")
    if not isinstance(suborg, Mapping):
        raise AuthorityError("authority proof requires suborg metadata")
    if not str(suborg.get("id") or "").strip() or suborg.get("isolated_per_tenant") is not True:
        raise AuthorityError("tenant must own one isolated Turnkey sub-organization")

    wallet = record.get("wallet")
    if not isinstance(wallet, Mapping):
        raise AuthorityError("authority proof requires wallet metadata")
    signer = _address(wallet.get("signer_address"), "wallet.signer_address")
    funder = _address(wallet.get("funder_address"), "wallet.funder_address")
    if wallet.get("deposit_wallet_deployed") is not True:
        raise AuthorityError("Deposit Wallet deployment is not verified")
    if wallet.get("approvals_ready") is not True:
        raise AuthorityError("trading approvals are not verified")
    if expected_signer_address and signer.lower() != _address(
        expected_signer_address, "expected_signer_address"
    ).lower():
        raise AuthorityError("authority signer does not match tenant signer")
    if expected_funder_address and funder.lower() != _address(
        expected_funder_address, "expected_funder_address"
    ).lower():
        raise AuthorityError("authority funder does not match tenant wallet")

    policy = record.get("policy")
    if not isinstance(policy, Mapping):
        raise AuthorityError("authority proof requires policy metadata")
    if policy.get("domain_name") != CLOB_DOMAIN_NAME:
        raise AuthorityError("authority policy has wrong EIP-712 domain")
    if str(policy.get("domain_version") or "") != CLOB_DOMAIN_VERSION:
        raise AuthorityError("authority policy has wrong EIP-712 domain version")
    if policy.get("primary_type") != CLOB_PRIMARY_TYPE:
        raise AuthorityError("authority policy has wrong EIP-712 primary type")
    if policy.get("chain_id") != POLYGON_CHAIN_ID:
        raise AuthorityError("authority policy has wrong chain id")
    if policy.get("signature_type") != DEPOSIT_WALLET_SIGNATURE_TYPE:
        raise AuthorityError("authority policy has wrong signature type")
    builder_code = str(policy.get("builder_code") or "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", builder_code):
        raise AuthorityError("authority policy requires an exact bytes32 builder code")
    if expected_builder_code is not None and builder_code.lower() != str(expected_builder_code).lower():
        raise AuthorityError("authority policy builder code mismatch")
    contracts = policy.get("verifying_contracts")
    if not isinstance(contracts, list) or not contracts:
        raise AuthorityError("authority policy requires verifying contracts")
    for contract in contracts:
        _address(contract, "policy.verifying_contracts[]")
    for flag in DENIED_POLICY_FLAGS:
        if policy.get(flag) is not False:
            raise AuthorityError(f"authority policy must explicitly deny {flag}")

    verification = record.get("verification")
    if not isinstance(verification, Mapping):
        raise AuthorityError("authority proof requires verification metadata")
    if verification.get("status") != "verified" or verification.get("source") != "turnkey_readonly":
        raise AuthorityError("authority proof is not a Turnkey read-only verification")
    claimed_digest = str(verification.get("proof_digest") or "")
    if claimed_digest != proof_digest(record):
        raise AuthorityError("authority proof digest mismatch")
    now_dt = _now_dt(now)
    verified_at = _parse_iso(verification.get("verified_at"), "verification.verified_at")
    if verified_at > now_dt:
        raise AuthorityError("authority verification timestamp is in the future")
    age = (now_dt - verified_at).total_seconds()
    if require_fresh and (max_proof_age_sec <= 0 or age > float(max_proof_age_sec)):
        raise AuthorityError("authority proof is stale")

    requested_side = None
    agent_summary = None
    if side is not None:
        requested_side = str(side).strip().upper()
        if requested_side not in SIDES:
            raise AuthorityError("authority side must be BUY or SELL")
        agents = record.get("agents")
        if not isinstance(agents, Mapping):
            raise AuthorityError("authority proof requires side-specific agents")
        agent = agents.get(requested_side)
        if not isinstance(agent, Mapping):
            raise AuthorityError(f"authority has no active {requested_side} agent")
        if agent.get("status") != "active" or agent.get("side") != requested_side:
            raise AuthorityError(f"authority {requested_side} agent is not active and side-bound")
        if not all(str(agent.get(k) or "").strip() for k in ("user_id", "policy_id", "api_public_key")):
            raise AuthorityError(f"authority {requested_side} agent identity is incomplete")
        capabilities = agent.get("capabilities")
        if not isinstance(capabilities, list) or set(capabilities) != set(ALLOWED_AGENT_CAPABILITIES):
            raise AuthorityError(f"authority {requested_side} agent has unexpected capabilities")
        expires_at = _parse_iso(agent.get("expires_at"), f"agents.{requested_side}.expires_at")
        if expires_at <= now_dt:
            raise AuthorityError(f"authority {requested_side} agent has expired")
        max_order = _positive_int(
            agent.get("maker_amount_ceiling"), f"agents.{requested_side}.maker_amount_ceiling"
        )
        expected_unit = (
            "pusd_collateral_micro" if requested_side == SIDE_BUY else "shares_micro"
        )
        if agent.get("maker_amount_unit") != expected_unit:
            raise AuthorityError(f"authority {requested_side} agent has wrong maker amount unit")
        if maker_amount_base_units is not None and _positive_int(
            maker_amount_base_units, "maker_amount_base_units"
        ) > max_order:
            raise AuthorityError("order exceeds user-root side-specific makerAmount ceiling")
        agent_summary = {
            "side": requested_side,
            "user_id": str(agent["user_id"]),
            "policy_id": str(agent["policy_id"]),
            "api_public_key": str(agent["api_public_key"]),
            "expires_at": agent["expires_at"],
            "maker_amount_ceiling": max_order,
            "maker_amount_unit": expected_unit,
            "builder_code": builder_code,
        }

    return {
        "schema": AUTHORITY_SCHEMA,
        "mode": MODE_TURNKEY_USER_ROOT,
        "tenant_id": tenant_id,
        "suborg_id": str(suborg["id"]),
        "signer_address": signer,
        "funder_address": funder,
        "verified_at": verification["verified_at"],
        "proof_age_sec": round(age, 3),
        "requested_side": requested_side,
        "agent": agent_summary,
        "root_user_controlled": True,
        "marketflow_root_member": False,
    }


def save_verified_authority(
    record: Mapping[str, Any],
    *,
    tenant_id: str,
    now: datetime | float | None = None,
) -> dict[str, Any]:
    """Persist only a structurally valid, fresh, gate-independent proof.

    Admission is still controlled separately by the gate file.  Saving a proof
    must never make a tenant live by itself.
    """
    summary = validate_user_root_authority(
        record,
        expected_tenant_id=tenant_id,
        now=now,
        require_gate=False,
        require_fresh=True,
    )
    _atomic_write(authority_path(tenant_id), dict(record))
    gstore.audit(
        "user_root_authority_verified",
        tenant_id=tenant_id,
        suborg_id=summary["suborg_id"],
        signer=_mask(summary["signer_address"]),
    )
    return summary


def public_status(tenant_id: str, *, now: datetime | float | None = None) -> dict[str, Any]:
    """Safe status for a status endpoint.  Never returns policy bodies or credentials."""
    try:
        record = load_authority(tenant_id)
        summary = validate_user_root_authority(
            record,
            expected_tenant_id=tenant_id,
            now=now,
            require_gate=False,
            require_fresh=False,
        )
        agents = record.get("agents") if isinstance(record.get("agents"), Mapping) else {}
        sides = sorted(
            side for side in SIDES
            if isinstance(agents.get(side), Mapping) and agents[side].get("status") == "active"
        )
        return {
            "mode": summary["mode"],
            "configured": True,
            "gate_open": user_root_enabled(),
            "proof_fresh": summary["proof_age_sec"] <= DEFAULT_MAX_PROOF_AGE_SEC,
            "execution_ready": bool(
                user_root_enabled()
                and summary["proof_age_sec"] <= DEFAULT_MAX_PROOF_AGE_SEC
                and sides
            ),
            "root_user_controlled": True,
            "marketflow_can_withdraw": False,
            "active_sides": sides,
            "verified_at": summary["verified_at"],
            "revoked": False,
        }
    except AuthorityError as exc:
        return {
            "mode": MODE_TURNKEY_USER_ROOT,
            "configured": os.path.exists(authority_path(tenant_id)),
            "gate_open": user_root_enabled(),
            "proof_fresh": False,
            "execution_ready": False,
            "root_user_controlled": None,
            "marketflow_can_withdraw": None,
            "active_sides": [],
            "revoked": "revoked" in str(exc).lower(),
            "error": str(exc),
        }


def mark_revocation_observed(
    tenant_id: str,
    *,
    turnkey_revoked_at: str,
    cancel_all_confirmed_at: str,
) -> dict[str, Any]:
    """Record a revocation only after Turnkey revoke AND CLOB cancel-all succeeded."""
    record = load_authority(tenant_id)
    if record.get("revoked_at"):
        return record
    _parse_iso(turnkey_revoked_at, "turnkey_revoked_at")
    _parse_iso(cancel_all_confirmed_at, "cancel_all_confirmed_at")
    updated = dict(record)
    updated["revoked_at"] = turnkey_revoked_at
    updated["cancel_all_confirmed_at"] = cancel_all_confirmed_at
    agents = {}
    for side, raw in dict(record.get("agents") or {}).items():
        agent = dict(raw) if isinstance(raw, Mapping) else {}
        agent["status"] = "revoked"
        agents[side] = agent
    updated["agents"] = agents
    verification = dict(updated.get("verification") or {})
    verification["proof_digest"] = ""
    updated["verification"] = verification
    verification["proof_digest"] = proof_digest(updated)
    _atomic_write(authority_path(tenant_id), updated)
    gstore.audit(
        "user_root_authority_revoked",
        tenant_id=tenant_id,
        turnkey_revoked_at=turnkey_revoked_at,
        cancel_all_confirmed_at=cancel_all_confirmed_at,
    )
    return updated


def _mask(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


# Side-specific ceiling used by the offline fixture (micro-units); any positive
# integer exercises the same checks.
FIXTURE_MAKER_CEILING = 10_000_000_000


def _fixture(now: datetime, *, tenant_id: str = "AUTH1") -> dict[str, Any]:
    expires = datetime.fromtimestamp(now.timestamp() + 3600, tz=timezone.utc)
    record: dict[str, Any] = {
        "schema": AUTHORITY_SCHEMA,
        "mode": MODE_TURNKEY_USER_ROOT,
        "tenant_id": tenant_id,
        "revoked_at": None,
        "root": {
            "owner": "user",
            "marketflow_member": False,
            "user_ids": ["user-root-1", "user-recovery-2"],
            "quorum_threshold": 2,
        },
        "suborg": {"id": "suborg-tenant-1", "isolated_per_tenant": True},
        "wallet": {
            "signer_address": "0x" + "1" * 40,
            "funder_address": "0x" + "2" * 40,
            "deposit_wallet_deployed": True,
            "approvals_ready": True,
        },
        "policy": {
            "domain_name": CLOB_DOMAIN_NAME,
            "domain_version": CLOB_DOMAIN_VERSION,
            "primary_type": CLOB_PRIMARY_TYPE,
            "chain_id": POLYGON_CHAIN_ID,
            "signature_type": DEPOSIT_WALLET_SIGNATURE_TYPE,
            "builder_code": BYTES32_ZERO,
            "verifying_contracts": ["0x" + "3" * 40],
            **{flag: False for flag in DENIED_POLICY_FLAGS},
        },
        "agents": {
            side: {
                "status": "active",
                "side": side,
                "user_id": f"agent-{side.lower()}",
                "policy_id": f"policy-{side.lower()}",
                "api_public_key": "02" + "4" * 64,
                "capabilities": sorted(ALLOWED_AGENT_CAPABILITIES),
                "maker_amount_ceiling": FIXTURE_MAKER_CEILING,
                "maker_amount_unit": (
                    "pusd_collateral_micro" if side == SIDE_BUY else "shares_micro"
                ),
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
            }
            for side in SIDES
        },
        "verification": {
            "status": "verified",
            "source": "turnkey_readonly",
            "verified_at": now.isoformat().replace("+00:00", "Z"),
            "proof_digest": "",
        },
    }
    record["verification"]["proof_digest"] = proof_digest(record)
    return record


def selftest() -> dict[str, bool]:
    """Offline invariants; caller redirects ``MARKETFLOW_GUARDIAN_ROOT`` before import."""
    now = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)
    record = _fixture(now)
    checks: dict[str, bool] = {}
    gate = os.path.join(gstore.GUARDIAN_ROOT, USER_ROOT_ENABLED_FILENAME)
    os.makedirs(gstore.GUARDIAN_ROOT, exist_ok=True)

    try:
        validate_user_root_authority(
            record, expected_tenant_id="AUTH1", side=SIDE_BUY, now=now
        )
        checks["closed_gate_refuses"] = False
    except AuthorityError:
        checks["closed_gate_refuses"] = True
    with open(gate, "w", encoding="utf-8") as handle:
        handle.write("selftest\n")
    summary = validate_user_root_authority(
        record,
        expected_tenant_id="AUTH1",
        side=SIDE_BUY,
        expected_signer_address="0x" + "1" * 40,
        expected_funder_address="0x" + "2" * 40,
        maker_amount_base_units=FIXTURE_MAKER_CEILING - 1_000_000,
        expected_builder_code=BYTES32_ZERO,
        now=now,
    )
    checks["valid_user_root_buy"] = summary["agent"]["side"] == SIDE_BUY
    checks["root_is_user_only"] = summary["root_user_controlled"] and not summary["marketflow_root_member"]

    def refused(mutator, **kwargs: Any) -> bool:
        changed = json.loads(json.dumps(record))
        mutator(changed)
        changed["verification"]["proof_digest"] = ""
        changed["verification"]["proof_digest"] = proof_digest(changed)
        try:
            validate_user_root_authority(
                changed,
                expected_tenant_id="AUTH1",
                side=kwargs.pop("side", SIDE_BUY),
                now=kwargs.pop("now", now),
                **kwargs,
            )
        except AuthorityError:
            return True
        return False

    checks["marketflow_root_member_refused"] = refused(
        lambda r: r["root"].update({"marketflow_member": True})
    )
    checks["private_key_refused"] = refused(
        lambda r: r.update({"private_key": "0x" + "9" * 64})
    )
    checks["batch_permission_refused"] = refused(
        lambda r: r["policy"].update({"allows_batch": True})
    )
    checks["wrong_domain_refused"] = refused(
        lambda r: r["policy"].update({"domain_name": "DepositWallet"})
    )
    checks["wrong_tenant_refused"] = refused(
        lambda r: r.update({"tenant_id": "OTHER"})
    )
    checks["wrong_wallet_refused"] = refused(
        lambda r: None,
        expected_funder_address="0x" + "8" * 40,
    )
    checks["unready_wallet_refused"] = refused(
        lambda r: r["wallet"].update({"approvals_ready": False})
    )
    checks["missing_side_agent_refused"] = refused(
        lambda r: r["agents"].pop(SIDE_BUY)
    )
    checks["cap_plus_one_refused"] = refused(
        lambda r: None,
        maker_amount_base_units=FIXTURE_MAKER_CEILING + 1,
    )
    checks["wrong_builder_refused"] = refused(
        lambda r: r["policy"].update({"builder_code": "0x" + "9" * 64}),
        expected_builder_code=BYTES32_ZERO,
    )
    checks["expired_agent_refused"] = refused(
        lambda r: r["agents"][SIDE_BUY].update({"expires_at": "2026-08-08T23:59:59Z"})
    )
    checks["revoked_refused"] = refused(
        lambda r: r.update({"revoked_at": "2026-08-09T00:00:00Z"})
    )
    stale_now = datetime.fromtimestamp(now.timestamp() + 121, tz=timezone.utc)
    checks["stale_proof_refused"] = refused(lambda r: None, now=stale_now)

    save_verified_authority(record, tenant_id="AUTH1", now=now)
    loaded = load_authority("AUTH1")
    checks["authority_roundtrip"] = loaded["tenant_id"] == "AUTH1"
    status = public_status("AUTH1", now=now)
    checks["public_status_redacted"] = (
        status["root_user_controlled"] is True
        and status["marketflow_can_withdraw"] is False
        and "policy" not in status
        and "api_public_key" not in json.dumps(status)
    )
    revoked = mark_revocation_observed(
        "AUTH1",
        turnkey_revoked_at="2026-08-09T00:01:00Z",
        cancel_all_confirmed_at="2026-08-09T00:01:01Z",
    )
    checks["revocation_requires_both_confirmations"] = (
        revoked["revoked_at"] and all(a["status"] == "revoked" for a in revoked["agents"].values())
    )
    return checks


if __name__ == "__main__":
    print("run authority.selftest through marketflow/guardian/selftest.py (temp runtime only)")
    raise SystemExit(2)
