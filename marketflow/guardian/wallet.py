"""Guardian encrypted credential storage for delegated mandates.

What is stored is a signing-enclave *agent* credential — an order-only, side-bound
key whose enclave policy cannot express a transfer — together with the venue API
credentials. No wallet private key, root key, mnemonic or seed is ever accepted:
encrypt_delegated_secrets refuses the blob if any of them is present. The user
holds the root key; this store never sees it.

Blobs are Fernet-encrypted on disk; plaintext exists only transiently in process
memory while a client is built. The master key lives outside the tenant tree
(MARKETFLOW_GUARDIAN_MASTER_KEY, 0600) and is created only by an explicit install
step. Losing it loses access to the stored credentials, which is recoverable by
re-delegating from the user's root key.
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


def encrypt_delegated_secrets(
    tenant_dir: str,
    secrets: dict[str, str],
    *,
    master_key: bytes | None = None,
) -> str:
    """Store a user-root delegation's agent credentials; refuse any wallet or root key.

    MarketFlow holds the P-256 API credentials of two agents whose Turnkey
    policies are order-only and side-bound. A raw wallet key, a root credential,
    or a single all-sides agent credential in this blob would be a custody
    regression, and is refused.
    """
    if str(secrets.get("key_backend") or "") != "turnkey_user_root":
        raise WalletError("delegated secrets require turnkey_user_root backend")
    forbidden = (
        "private_key", "api_private_key", "turnkey_agent_private_key",
        "turnkey_root_private_key", "root_private_key", "mnemonic", "seed",
    )
    if any(str(secrets.get(field) or "").strip() for field in forbidden):
        raise WalletError("wallet key, root credential or all-sides agent material is forbidden in delegated secrets")
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
    """Load the mandate's agent credentials into memory. Caller must not persist them."""
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
