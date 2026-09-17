"""Guardian per-tenant arm writing (money-surface; design approved by the owner
2026-07-21, the design note).

This is the ONLY place a guardian tenant arm file is written. Semantics mirror
the bridge's single-tenant arm write, adapted to hosted tenants:

  * written_by = "guardian_service"; live_ack = GUARDIAN_TENANT_APPROVES_AUTO_EXIT;
    tenant_id bound into the file (a copied arm file cannot arm another tenant).
  * mode is one of:
      - "exit_only" (default, v0) — the automated side can only ever SELL.
      - "entry_flb" (260725) — SELL, plus automated BUY from the `flb_harvester`
        source ONLY. Deliberately NOT "full": a named-source mode means no future
        signal source silently inherits the right to open positions on a hosted
        user's money. Requires its own ops gate (GUARDIAN_ENTRY_ENABLED) on top of
        GUARDIAN_LIVE_ENABLED, so entry can be switched off fleet-wide without
        disarming exits — the safe direction stays available during an incident.
    "full" is not writable here at all.
  * arm is refused while the global GUARDIAN_LIVE_ENABLED gate is absent — even
    with the owner's design approval, ops must explicitly open the gate first.
  * the trigger is the USER's explicit Telegram confirmation (two-step command);
    guardian never self-arms a tenant on its own schedule.
  * expiry 30 days: automation silently stopping is worse than re-confirming, so
    the user re-confirms monthly; disarm is instant and unconditional.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import executor as gexec  # noqa: E402

ARM_TTL_DAYS = 30

# The only modes this writer will ever produce. "full" is intentionally absent:
# a hosted user's automation is scoped to named capabilities, never to "anything".
ARM_MODE_EXIT_ONLY = "exit_only"
ARM_MODE_ENTRY_FLB = "entry_flb"
WRITABLE_ARM_MODES = (ARM_MODE_EXIT_ONLY, ARM_MODE_ENTRY_FLB)


class ArmError(Exception):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def arm_tenant(tenant_id: str, *, mode: str = ARM_MODE_EXIT_ONLY) -> dict[str, Any]:
    """Write the tenant's arm file. Caller (http_api) must have already verified the
    user's explicit confirmation — and for `entry_flb`, a confirmation whose wording
    said that automation will BUY. Fail-closed on every doubt."""
    if mode not in WRITABLE_ARM_MODES:
        raise ArmError(f"refusing to write arm mode {mode!r}; allowed: {WRITABLE_ARM_MODES}")
    if not gstore.live_enabled():
        raise ArmError("GUARDIAN_LIVE_ENABLED absent; arm refused (ops gate closed)")
    if mode == ARM_MODE_ENTRY_FLB and not gstore.entry_enabled():
        raise ArmError("GUARDIAN_ENTRY_ENABLED absent; entry arm refused (ops gate closed)")
    entry = gstore.load_registry()["tenants"].get(tenant_id)
    if entry is None:
        raise ArmError(f"unknown tenant {tenant_id}")
    if entry.get("authority_mode") == "turnkey_user_root":
        from marketflow.guardian import authority as gauth

        try:
            record = gauth.load_authority(tenant_id)
            sides = (gauth.SIDE_SELL, gauth.SIDE_BUY) if mode == ARM_MODE_ENTRY_FLB else (gauth.SIDE_SELL,)
            for side in sides:
                gauth.validate_user_root_authority(
                    record,
                    expected_tenant_id=tenant_id,
                    side=side,
                    expected_signer_address=entry.get("signer_address"),
                    expected_funder_address=entry.get("funder_address"),
                )
        except gauth.AuthorityError as exc:
            raise ArmError(f"user-root authority refused: {exc}") from exc
    now = datetime.now(timezone.utc)
    from marketflow.execution import orders as pmx  # noqa: E402  (schema version constant)
    obj = {
        "schema_version": pmx.ARM_STATE_SCHEMA_VERSION,
        "armed": True,
        "mode": mode,
        "live_ack": gexec.GUARDIAN_LIVE_ACK,
        "written_by": gexec.GUARDIAN_ARM_WRITER,
        "tenant_id": tenant_id,
        "budget_epoch": f"guardian-{tenant_id}-{_iso(now)}",
        "budget_epoch_started_at": _iso(now),
        "armed_at": _iso(now),
        "expires_at": _iso(now + timedelta(days=ARM_TTL_DAYS)),
    }
    path = gexec.tenant_arm_file(tenant_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    gstore.audit("tenant_armed", tenant_id=tenant_id, mode=mode,
                 expires_at=obj["expires_at"])
    return {"armed": True, "mode": mode, "expires_at": obj["expires_at"]}


def disarm_tenant(tenant_id: str) -> dict[str, Any]:
    """Disarm unconditionally (no gate — turning OFF is always allowed)."""
    path = gexec.tenant_arm_file(tenant_id)
    now = datetime.now(timezone.utc)
    obj = {
        "armed": False,
        "mode": "off",
        "written_by": gexec.GUARDIAN_ARM_WRITER,
        "tenant_id": tenant_id,
        "disarmed_at": _iso(now),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    gstore.audit("tenant_disarmed", tenant_id=tenant_id)
    return {"armed": False, "mode": "off"}


def arm_status(tenant_id: str) -> dict[str, Any]:
    """Read-only view of the tenant arm through the SAME validator the executor
    uses (no parallel truth)."""
    from marketflow.execution import orders as pmx  # noqa: E402

    state = pmx.load_arm_state(
        gexec.tenant_arm_file(tenant_id), pmx.FuseCaps(),
        expected_writer=gexec.GUARDIAN_ARM_WRITER,
        expected_ack=gexec.GUARDIAN_LIVE_ACK,
        expected_tenant=tenant_id,
    )
    return {"armed": bool(state.get("armed")), "mode": state.get("mode"),
            "expires_at": state.get("expires_at"),
            "live_enabled_globally": gstore.live_enabled()}
