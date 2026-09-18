"""Guardian mandate registration: non-custodial user-root delegation.

Every mandate is registered from a user-root delegation: the account holder
holds the root quorum of their own Turnkey sub-organization, the enclave holds
the trading key, and MarketFlow holds only the API credentials of two side-bound
agents the root created. MarketFlow never receives a private key.

Registration is the last step, not the first. The sub-organization, the Deposit
Wallet deploy, the exchange approvals, the agents and their policies are all the
account holder's to set up with root; this module accepts the result only when a
fresh read-only authority proof validates for both sides and matches the agent
credentials handed over. Any mismatch refuses the registration, and a mandate is
`ready` from the moment it exists.
"""

from __future__ import annotations

from typing import Any

from marketflow.guardian import store as gstore
from marketflow.guardian import wallet as gwallet


def register_user_root_delegation(
    tenant_id: str,
    *,
    authority_record: dict[str, Any],
    delegated_secrets: dict[str, str],
    master_key: bytes | None = None,
) -> dict[str, Any]:
    """Register a mandate after root-side setup and read-only verification.

    Never creates a Turnkey root, never accepts a private key, and never converts
    an existing record in place. The wallet and both side-specific agents must
    already exist and be verified through Turnkey's read-only API.
    """
    from marketflow.guardian import authority as gauth

    tid = str(tenant_id or "")
    gstore.tenant_dir(tid)  # validates the id before anything is read or written
    existing = gstore.get_tenant(tid)
    if existing is not None:
        if existing.get("authority_mode") == gauth.MODE_TURNKEY_USER_ROOT:
            return existing
        raise gstore.StoreError(
            "an existing record that is not a user-root delegation cannot be "
            "converted in place; register a new mandate"
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


def deposit_address(tenant_id: str) -> str | None:
    """This mandate's Polymarket Deposit Wallet, or None when none is on record.

    **Never falls back to the signer EOA.** The venue credits collateral through
    the Deposit Wallet: stablecoin sent there is wrapped into the venue's
    collateral token by its relayer. The same transfer sent to the bare signer
    EOA is not credited to the trading account. Returning no address at all is
    strictly better than returning one that strands the money.
    """
    entry = gstore.get_tenant(tenant_id)
    if entry is None:
        return None
    return entry.get("funder_address")


def _mask(addr: str | None) -> str | None:
    if not addr:
        return None
    body = addr[2:] if addr.lower().startswith("0x") else addr
    return f"0x{body[:4]}…{body[-4:]}" if len(body) > 8 else "0x****"
