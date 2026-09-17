"""Guardian encrypted tenant-secret storage.

Custody model: we GENERATE a fresh EOA per tenant on the VPS;
the user funds its Polymarket Deposit Wallet. The private key + CLOB API creds
are Fernet-encrypted on disk; plaintext exists only transiently in process
memory when building an SDK client (pmx.build_secure_client accepts the secrets
dict directly — no plaintext file is ever written).

Master key: one symmetric key for the whole guardian store, kept OUTSIDE the
tenant tree (~/.marketflow/secrets/guardian_master_key.txt, 0600, created only by an
explicit install step). Losing it = losing every hosted wallet, so ops must back it up;
rotating it = re-encrypt loop (v0: manual, see README).
"""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet

MASTER_KEY_FILE = os.environ.get(
    "MARKETFLOW_GUARDIAN_MASTER_KEY",
    os.path.join(os.path.expanduser("~"), ".marketflow", "secrets", "guardian_master_key.txt"),
)

SECRETS_BLOB = "secrets.enc"
FUNDER_FILE = "funder_address.txt"  # public on-chain address; not a secret


class WalletError(Exception):
    pass


def _ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def load_master_key(*, path: str = MASTER_KEY_FILE, create: bool = False) -> bytes:
    """Read the guardian master key; never create it on a hot execution path."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read().strip()
        if not key:
            raise WalletError("guardian master key file is empty; refusing to operate")
        return key
    if not create:
        raise WalletError("guardian master key missing")
    _ensure_dir(os.path.dirname(path))
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key + b"\n")
    finally:
        os.close(fd)
    return key


def initialize_master_key(*, path: str = MASTER_KEY_FILE) -> bytes:
    """Explicit install-time key creation. Runtime callers use ``load_master_key``."""
    return load_master_key(path=path, create=True)


def create_eoa() -> dict[str, str]:
    """Generate a fresh EOA. Entropy from eth_account (os.urandom-backed)."""
    from eth_account import Account

    acct = Account.create()
    return {"address": acct.address, "private_key": acct.key.hex()}


def encrypt_secrets(tenant_dir: str, secrets: dict[str, str], *, master_key: bytes | None = None) -> str:
    """Write the tenant's secret material as one Fernet blob. Never plaintext."""
    if "private_key" not in secrets or not secrets["private_key"]:
        raise WalletError("refusing to store a secrets blob without a private_key")
    key = master_key if master_key is not None else load_master_key(create=False)
    _ensure_dir(tenant_dir)
    blob = Fernet(key).encrypt(json.dumps(secrets, sort_keys=True).encode("utf-8"))
    path = os.path.join(tenant_dir, SECRETS_BLOB)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def encrypt_delegated_secrets(
    tenant_dir: str,
    secrets: dict[str, str],
    *,
    master_key: bytes | None = None,
) -> str:
    """Store user-root delegated agents without accepting an EOA/root key.

    MarketFlow still holds P-256 agent credentials, but their Turnkey policies are
    order-only and side-bound.  A raw EOA key, platform-root key, or legacy
    all-sides agent in this blob is a custody regression and is refused.
    """
    if str(secrets.get("key_backend") or "") != "turnkey_user_root":
        raise WalletError("delegated secrets require turnkey_user_root backend")
    forbidden = (
        "private_key", "api_private_key", "turnkey_agent_private_key",
        "turnkey_root_private_key", "root_private_key", "mnemonic", "seed",
    )
    if any(str(secrets.get(field) or "").strip() for field in forbidden):
        raise WalletError("EOA/root/legacy agent material forbidden in delegated secrets")
    required = (
        "turnkey_organization_id", "turnkey_signer_address", "funder_address",
        "api_key", "api_secret", "passphrase",
        "turnkey_entry_agent_private_key", "turnkey_entry_agent_public_key",
        "turnkey_exit_agent_private_key", "turnkey_exit_agent_public_key",
    )
    missing = [field for field in required if not str(secrets.get(field) or "").strip()]
    if missing:
        raise WalletError(f"delegated secrets missing {missing}")
    key = master_key if master_key is not None else load_master_key(create=False)
    _ensure_dir(tenant_dir)
    blob = Fernet(key).encrypt(json.dumps(secrets, sort_keys=True).encode("utf-8"))
    path = os.path.join(tenant_dir, SECRETS_BLOB)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def decrypt_secrets(tenant_dir: str, *, master_key: bytes | None = None) -> dict[str, str]:
    """Load the tenant's secrets into memory. Caller must not persist them."""
    key = master_key if master_key is not None else load_master_key(create=False)
    path = os.path.join(tenant_dir, SECRETS_BLOB)
    if not os.path.exists(path):
        raise WalletError(f"no secrets blob in {tenant_dir}")
    with open(path, "rb") as f:
        blob = f.read()
    data = json.loads(Fernet(key).decrypt(blob).decode("utf-8"))
    if not isinstance(data, dict):
        raise WalletError("secrets blob decrypted to a non-object")
    return {str(k): str(v) for k, v in data.items()}


def write_funder(tenant_dir: str, address: str) -> None:
    _ensure_dir(tenant_dir)
    with open(os.path.join(tenant_dir, FUNDER_FILE), "w", encoding="utf-8") as f:
        f.write(address.strip() + "\n")


def read_funder(tenant_dir: str) -> str | None:
    path = os.path.join(tenant_dir, FUNDER_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        addr = f.read().strip()
    return addr or None


def has_secrets(tenant_dir: str) -> bool:
    return os.path.exists(os.path.join(tenant_dir, SECRETS_BLOB))
