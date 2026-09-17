"""Guardian tenant onboarding and non-custodial delegation registration.

Legacy hosted-wallet creation is retained only for disaster recovery of old
installations and is disabled by default. New production tenants must use the
user-root Turnkey flow, where MarketFlow never receives a root private key.

Non-negotiable ordering (fail-closed): a tenant is `ready` (tradeable) ONLY when
its Deposit Wallet is deployed, CLOB creds are derived, AND on-chain trading
approvals are in place. Until then every execution path plans dry_run regardless
of arm state.

Deposit-wallet reality (verified against the SDK 2026-07-21): Polymarket routes
collateral through a Deposit Wallet — a proxy of the signer EOA. A FRESH EOA has
no Deposit Wallet yet, and SecureClient.create auto-attempts to DEPLOY one, which
needs gasless infra: a Builder/Relayer API key. That key is a MARKETFLOW-SIDE infra
credential (not a user secret) at ~/.marketflow/secrets/guardian_builder_api_key.txt;
it authorizes gasless deploy + relayed setup, it does NOT let anyone move a user's
funds (that stays gated by FORBIDDEN_SDK_METHODS + the human-approved withdrawal
path). The address a user funds (USDC.e on Polygon) is the resolved Deposit
Wallet, surfaced once deploy succeeds.
"""

from __future__ import annotations

import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import wallet as gwallet  # noqa: E402
from marketflow.guardian import store as gstore  # noqa: E402


class HostedOnboardingDisabled(RuntimeError):
    """Raised before key generation when the legacy custody path is closed."""


def legacy_hosted_onboarding_enabled() -> bool:
    return os.environ.get("MARKETFLOW_ALLOW_LEGACY_HOSTED_ONBOARDING", "").strip().lower() in {
        "1", "true", "yes",
    }

# MarketFlow-side infra credential for gasless Deposit Wallet deploy + relayed setup.
# NOT a user secret and NOT a funds-movement key. Absent -> ready-transition
# fails soft (tenant stays `created`, no live trading), never crashes.
# Self-serve issuance: polymarket.com/settings?tab=builder -> three values.
# File format: JSON {"key":..,"secret":..,"passphrase":..} OR three lines
# (key / secret / passphrase).
BUILDER_API_KEY_FILE = os.path.join(
    os.environ.get(
        "MARKETFLOW_GUARDIAN_SECRETS_DIR",
        os.path.join(os.path.expanduser("~"), ".marketflow", "secrets"),
    ),
    "guardian_builder_api_key.txt",
)


def _builder_api_key() -> Any | None:
    """Load Builder credentials as the SDK's BuilderApiKey, or None if absent."""
    try:
        with open(BUILDER_API_KEY_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        from polymarket.auth import BuilderApiKey  # type: ignore
    except Exception:
        return None
    try:
        import json as _json
        doc = _json.loads(raw)
        if isinstance(doc, dict) and doc.get("key") and doc.get("secret") and doc.get("passphrase"):
            return BuilderApiKey(key=str(doc["key"]), secret=str(doc["secret"]),
                                 passphrase=str(doc["passphrase"]))
    except ValueError:
        pass
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 3:
        return BuilderApiKey(key=lines[0], secret=lines[1], passphrase=lines[2])
    return None


def onboard(chat_id: int | str, *, master_key: bytes | None = None) -> dict[str, Any]:
    """Create a hosted wallet for a chat if none exists; return the tenant entry
    (idempotent — an existing tenant is returned untouched)."""
    if not legacy_hosted_onboarding_enabled():
        raise HostedOnboardingDisabled(
            "legacy hosted-wallet onboarding is disabled; use user-root delegation"
        )
    tid = gstore.tenant_id_for_chat(chat_id)
    existing = gstore.get_tenant(chat_id)
    if existing is not None:
        return existing
    eoa = gwallet.create_eoa()
    tdir = gstore.tenant_dir(tid)
    # v0 stores just the private key; CLOB creds are derived on the ready
    # transition (needs network) and merged into the same blob then.
    gwallet.encrypt_secrets(tdir, {"private_key": eoa["private_key"]}, master_key=master_key)
    gwallet.write_funder(tdir, eoa["address"])  # signer address; deposit wallet resolved later
    entry = {
        "tenant_id": tid,
        "chat_id": str(chat_id),
        "status": "created",
        "authority_mode": "legacy_local",
        "custody_mode": "hosted_legacy",
        "signer_address": eoa["address"],
        "funder_address": None,
        "created_at": gstore.iso_now(),
    }
    gstore.upsert_tenant(entry)
    gstore.audit("tenant_created", tenant_id=tid, signer=_mask(eoa["address"]))
    return entry


def register_user_root_delegation(
    chat_id: int | str,
    *,
    authority_record: dict[str, Any],
    delegated_secrets: dict[str, str],
    master_key: bytes | None = None,
) -> dict[str, Any]:
    """Register a fresh non-custodial tenant after root-side verification.

    This function never creates a Turnkey root, never accepts an EOA private
    key, and never migrates a legacy hosted wallet in place. The wallet and both
    side-specific agents must already exist and be verified through Turnkey's
    read-only API by the user-root provisioning flow.
    """
    from marketflow.guardian import authority as gauth

    tid = gstore.tenant_id_for_chat(chat_id)
    existing = gstore.get_tenant(chat_id)
    if existing is not None:
        if existing.get("authority_mode") == gauth.MODE_TURNKEY_USER_ROOT:
            return existing
        raise gstore.StoreError(
            "legacy hosted wallet cannot be converted in place; register a fresh user-root wallet"
        )
    buy = gauth.validate_user_root_authority(
        authority_record, expected_tenant_id=tid, side=gauth.SIDE_BUY,
        require_gate=False,
    )
    sell = gauth.validate_user_root_authority(
        authority_record, expected_tenant_id=tid, side=gauth.SIDE_SELL,
        require_gate=False,
    )
    if str(delegated_secrets.get("turnkey_organization_id") or "") != buy["suborg_id"]:
        raise gauth.AuthorityError("delegated secret suborg does not match authority proof")
    if str(delegated_secrets.get("turnkey_signer_address") or "").lower() != buy["signer_address"].lower():
        raise gauth.AuthorityError("delegated signer does not match authority proof")
    if str(delegated_secrets.get("funder_address") or "").lower() != buy["funder_address"].lower():
        raise gauth.AuthorityError("delegated funder does not match authority proof")
    if str(delegated_secrets.get("turnkey_entry_agent_public_key") or "").lower() != str(
        buy["agent"]["api_public_key"]
    ).lower():
        raise gauth.AuthorityError("delegated BUY agent does not match authority proof")
    if str(delegated_secrets.get("turnkey_exit_agent_public_key") or "").lower() != str(
        sell["agent"]["api_public_key"]
    ).lower():
        raise gauth.AuthorityError("delegated SELL agent does not match authority proof")

    tdir = gstore.tenant_dir(tid)
    gwallet.encrypt_delegated_secrets(
        tdir, delegated_secrets, master_key=master_key,
    )
    gauth.save_verified_authority(authority_record, tenant_id=tid)
    gwallet.write_funder(tdir, buy["funder_address"])
    entry = {
        "tenant_id": tid,
        "chat_id": str(chat_id),
        "status": "ready",
        "authority_mode": gauth.MODE_TURNKEY_USER_ROOT,
        "custody_mode": "noncustodial_user_root",
        "signer_address": buy["signer_address"],
        "funder_address": buy["funder_address"],
        "deposit_wallet_deployed": True,
        "approvals_ok": True,
        "created_at": gstore.iso_now(),
        "authority_verified_at": buy["verified_at"],
    }
    gstore.upsert_tenant(entry)
    gstore.audit(
        "tenant_user_root_registered", tenant_id=tid,
        signer=_mask(buy["signer_address"]), funder=_mask(buy["funder_address"]),
    )
    return entry


def deposit_address(chat_id: int | str) -> str | None:
    """Deposit address = this mandate's Polymarket Deposit Wallet, or None when
    it has not been deployed yet.

    **Never falls back to the signer EOA.** The venue's collateral onramp only
    recognises the Deposit Wallet: stablecoin sent there is wrapped automatically
    by the official relayer (measured on-chain: handleOps moves it into the
    collateral token seconds after arrival). The same transfer sent to a bare
    signer EOA is claimed by nobody and sits there. Returning no address at all is
    strictly better than returning one that swallows the money.
    """
    entry = gstore.get_tenant(chat_id)
    if entry is None:
        return None
    return entry.get("funder_address")


def try_ready(chat_id: int | str, *, master_key: bytes | None = None, min_collateral_usd: float = 1.0) -> dict[str, Any]:
    """Attempt created/funded -> ready: build the SDK client (derives CLOB creds),
    check collateral + approvals. Fail-closed: any missing step keeps the earlier
    status. Requires the polymarket SDK (VPS python env)."""
    from marketflow.execution import orders as pmx  # type: ignore

    tid = gstore.tenant_id_for_chat(chat_id)
    entry = gstore.get_tenant(chat_id)
    if entry is None:
        raise gstore.StoreError("onboard before try_ready")
    if entry.get("authority_mode") == "turnkey_user_root":
        raise gstore.StoreError(
            "user-root readiness must be reconciled from Turnkey; hosted try_ready is forbidden"
        )
    tdir = gstore.tenant_dir(tid)
    secrets = gwallet.decrypt_secrets(tdir, master_key=master_key)

    from polymarket import SecureClient  # type: ignore
    # api_key authorizes the gasless Deposit Wallet deploy on first use; without
    # it SecureClient.create raises for a fresh EOA (verified 2026-07-21).
    builder_key = _builder_api_key()
    create_kwargs: dict[str, Any] = {"private_key": secrets["private_key"]}
    if builder_key:
        create_kwargs["api_key"] = builder_key
    client = SecureClient.create(**create_kwargs)
    try:
        funder = getattr(client, "wallet", None) or getattr(client, "funder", None)
        funder = str(funder) if funder else entry.get("signer_address")
        # Persist derived creds so the executor never re-derives on the hot path.
        creds = _extract_creds(client)
        if creds:
            secrets.update(creds)
        secrets["funder_address"] = funder
        gwallet.encrypt_secrets(tdir, secrets, master_key=master_key)
        gwallet.write_funder(tdir, funder)
        entry["funder_address"] = funder

        collateral = _collateral_usd(client)
        entry["collateral_usd"] = collateral
        approvals_ok = _approvals_ready(client)
        entry["approvals_ok"] = approvals_ok
        if collateral is not None and collateral >= min_collateral_usd and approvals_ok:
            entry["status"] = "ready"
        elif collateral is not None and collateral >= min_collateral_usd:
            entry["status"] = "funded"
        # else stays created
        gstore.upsert_tenant(entry)
        gstore.audit("tenant_status", tenant_id=tid, status=entry["status"],
                     collateral_usd=collateral, approvals_ok=approvals_ok)
        return entry
    finally:
        try:
            client.close()
        except Exception:
            pass


def _extract_creds(client: Any) -> dict[str, str] | None:
    creds = getattr(client, "credentials", None) or getattr(client, "creds", None)
    if creds is None:
        return None
    key = getattr(creds, "key", None) or getattr(creds, "api_key", None)
    secret = getattr(creds, "secret", None) or getattr(creds, "api_secret", None)
    passphrase = getattr(creds, "passphrase", None)
    if not (key and secret and passphrase):
        return None
    return {"api_key": str(key), "api_secret": str(secret), "passphrase": str(passphrase)}


def _collateral_usd(client: Any) -> float | None:
    try:
        ba = client.get_balance_allowance(asset_type="COLLATERAL")
    except Exception:
        return None
    bal = getattr(ba, "balance", None)
    try:
        # SDK returns base units (6-dp USDC) as int/str; normalize to dollars.
        return round(float(bal) / 1_000_000.0, 6) if bal is not None else None
    except (TypeError, ValueError):
        return None


def _approvals_ready(client: Any) -> bool:
    fn = getattr(client, "is_gasless_ready", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False


def _mask(addr: str | None) -> str | None:
    if not addr:
        return None
    body = addr[2:] if addr.lower().startswith("0x") else addr
    return f"0x{body[:4]}…{body[-4:]}" if len(body) > 8 else "0x****"
