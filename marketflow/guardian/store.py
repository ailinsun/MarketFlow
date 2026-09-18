"""Guardian mandate registry + append-only audit trail.

Single-writer contract: only the guardian service process writes these files; a
front end talks to the service over the loopback HTTP seam (`http_api.py`),
never to the files. Atomic JSON writes; the audit trail is append-only JSONL
with flock.

A mandate is keyed by its tenant id, an opaque identifier the deployment
assigns. Lifecycle status:
    ready              registered from a validated user-root authority proof
                       (wallet deployed, approvals done, agents active)
    paused             the account holder paused automation (funds stay in
                       their wallet; no decisions are acted on)
There is no status before `ready`: a mandate is registered only once its
authority proof validates, and any doubt refuses the registration.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
GUARDIAN_ROOT = os.environ.get(
    "MARKETFLOW_GUARDIAN_ROOT", runtime_path("guardian")
)
TENANTS_ROOT = os.path.join(GUARDIAN_ROOT, "tenants")
REGISTRY_FILE = os.path.join(GUARDIAN_ROOT, "registry.json")
AUDIT_FILE = os.path.join(GUARDIAN_ROOT, "audit.jsonl")
# Money-surface gate: while this file is ABSENT every mandate's arm request is
# refused and all execution stays dry_run. The operator creates it deliberately;
# nothing in the service ever does.
LIVE_ENABLED_FILE = os.path.join(GUARDIAN_ROOT, "GUARDIAN_LIVE_ENABLED")
# Second, independent money-surface gate covering AUTOMATED ENTRY only.
# Absent => no tenant may open a position, regardless of their arm mode; exits are
# unaffected. See store.entry_enabled() for why the two gates are separate.
ENTRY_ENABLED_FILE = os.path.join(GUARDIAN_ROOT, "GUARDIAN_ENTRY_ENABLED")

REGISTRY_SCHEMA = "guardian-registry-v0.1"
STATUSES = ("ready", "paused")

_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StoreError(Exception):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def tenant_dir(tenant_id: str) -> str:
    if not _TENANT_ID_RE.match(tenant_id):
        raise StoreError(f"unsafe tenant_id {tenant_id!r}")
    return os.path.join(TENANTS_ROOT, tenant_id)


def _atomic_write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def append_jsonl(path: str, row: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_registry(path: str = REGISTRY_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"schema": REGISTRY_SCHEMA, "tenants": {}}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("tenants"), dict):
        raise StoreError("guardian registry malformed; refusing to operate on it")
    return doc


def save_registry(doc: dict[str, Any], path: str = REGISTRY_FILE) -> None:
    doc["schema"] = REGISTRY_SCHEMA
    doc["updated_at"] = iso_now()
    _atomic_write_json(path, doc)


def get_tenant(tenant_id: str, *, registry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    tid = str(tenant_id or "")
    if not _TENANT_ID_RE.match(tid):
        raise StoreError(f"unsafe tenant_id {tenant_id!r}")
    doc = registry if registry is not None else load_registry()
    return doc["tenants"].get(tid)


def upsert_tenant(entry: dict[str, Any], *, path: str = REGISTRY_FILE) -> dict[str, Any]:
    tid = entry.get("tenant_id")
    if not tid or not _TENANT_ID_RE.match(str(tid)):
        raise StoreError(f"unsafe tenant_id in entry: {tid!r}")
    if entry.get("status") not in STATUSES:
        raise StoreError(f"unknown status {entry.get('status')!r}")
    doc = load_registry(path)
    doc["tenants"][str(tid)] = entry
    save_registry(doc, path)
    return entry


def audit(event: str, *, tenant_id: str | None = None, path: str = AUDIT_FILE, **fields: Any) -> None:
    """Append-only audit trail: registration / arm / order / authority events.
    Never contains secret material (callers pass ids, masked addresses, sizes)."""
    append_jsonl(path, {"ts": iso_now(), "event": event, "tenant_id": tenant_id, **fields})


def live_enabled() -> bool:
    return os.path.exists(LIVE_ENABLED_FILE)


def entry_enabled() -> bool:
    """Fleet-wide gate for AUTOMATED ENTRY (BUY), separate from LIVE_ENABLED.

    Two gates, not one, so the two directions can be controlled independently:
    removing this file stops every tenant opening new positions while exits keep
    running normally. During an incident the safe direction must stay available —
    a single gate would force a choice between "keeps buying" and "cannot sell".
    Entry additionally requires the tenant's own arm to be mode=entry_allowlisted."""
    return os.path.exists(ENTRY_ENABLED_FILE)
