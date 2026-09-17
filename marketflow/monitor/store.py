"""Subscription registry plus alert de-duplication and rolling-high state,
persisted as atomically written JSON.

Two files, both under runtime/alerts/, deliberately separate from the execution
stack's runtime/ so the two cannot corrupt each other:
- subscribers.json : {chat_id → {addresses, username, created_at, updated_at, paused}}
- state.json       : {telegram_offset, watch: {"<chat>|<addr>": WatchState}}
    WatchState = {seeded, positions: {asset → {rolling_high}}, fired: {dedupKey → {bucket, at}}}

Atomic writes: write a .tmp file then os.replace it (POSIX rename is atomic), so
a concurrent reader or a crash always sees either the complete old version or the
complete new one, never a half-written file.

This layer holds no business logic at all: read, write, de-duplicate.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from marketflow.paths import PROJECT_DIR, runtime_path
OUT_DIR = runtime_path("monitor")
SUBSCRIBERS_PATH = os.path.join(OUT_DIR, "subscribers.json")
STATE_PATH = os.path.join(OUT_DIR, "state.json")

SUBSCRIBERS_SCHEMA = "marketflow-alerts-subscribers-v0.1"
STATE_SCHEMA = "marketflow-alerts-state-v0.1"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_dir() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)


def _atomic_write_json(path: str, data: Any) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=OUT_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)  # atomic
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Fail soft: a corrupt file must not take the service down. Fall back to
        # the default; the next write repairs it.
        return default


# ---------------------------------------------------------------- subscribers


def load_subscribers() -> dict[str, dict[str, Any]]:
    """Return {chat_id (str): subscriber dict}."""
    doc = _read_json(SUBSCRIBERS_PATH, {})
    subs = doc.get("subscribers") if isinstance(doc, dict) else None
    return subs if isinstance(subs, dict) else {}


def save_subscribers(subs: dict[str, dict[str, Any]]) -> None:
    _atomic_write_json(
        SUBSCRIBERS_PATH,
        {"schema": SUBSCRIBERS_SCHEMA, "updated_at": iso_now(), "subscribers": subs},
    )


def get_subscriber(subs: dict[str, dict[str, Any]], chat_id: int | str) -> dict[str, Any]:
    key = str(chat_id)
    sub = subs.get(key)
    if not isinstance(sub, dict):
        sub = {
            "chat_id": chat_id,
            "username": "",
            "addresses": [],
            "created_at": iso_now(),
            "updated_at": iso_now(),
            "paused": False,
        }
        subs[key] = sub
    return sub


def add_address(sub: dict[str, Any], address: str) -> bool:
    """Add one watched address, de-duplicated case-insensitively. Returns True
    when it was newly added, False when it was already there."""
    addr = address.strip().lower()
    addrs = sub.setdefault("addresses", [])
    if addr in addrs:
        return False
    addrs.append(addr)
    sub["updated_at"] = iso_now()
    return True


def add_market(sub: dict[str, Any], rec: dict[str, Any]) -> bool:
    """Add one watched market, de-duplicated by gamma id. Returns True when it
    was newly added."""
    markets = sub.setdefault("markets", [])
    if any(isinstance(m, dict) and m.get("id") == rec.get("id") for m in markets):
        return False
    markets.append(rec)
    sub["updated_at"] = iso_now()
    return True


def remove_market(sub: dict[str, Any], gamma_id: str) -> dict[str, Any] | None:
    """Remove a watched market by gamma id. Returns the removed record, or None
    when it was not in the list."""
    markets = sub.setdefault("markets", [])
    for m in list(markets):
        if isinstance(m, dict) and m.get("id") == str(gamma_id):
            markets.remove(m)
            sub["updated_at"] = iso_now()
            return m
    return None


def remove_address(sub: dict[str, Any], address: str) -> bool:
    addr = address.strip().lower()
    addrs = sub.setdefault("addresses", [])
    if addr not in addrs:
        return False
    addrs.remove(addr)
    sub["updated_at"] = iso_now()
    return True


def all_watched_addresses(subs: dict[str, dict[str, Any]]) -> list[str]:
    """Every watched address across all subscribers, de-duplicated, so the
    monitoring loop can fetch them in one batch."""
    seen: set[str] = set()
    for sub in subs.values():
        if sub.get("paused"):
            continue
        for a in sub.get("addresses", []):
            seen.add(str(a).strip().lower())
    return sorted(seen)


# ---------------------------------------------------------------- runtime state


def load_state() -> dict[str, Any]:
    doc = _read_json(STATE_PATH, {})
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("schema", STATE_SCHEMA)
    doc.setdefault("telegram_offset", 0)
    doc.setdefault("watch", {})
    return doc


def save_state(state: dict[str, Any]) -> None:
    state["schema"] = STATE_SCHEMA
    state["updated_at"] = iso_now()
    _atomic_write_json(STATE_PATH, state)


def watch_key(chat_id: int | str, address: str) -> str:
    return f"{chat_id}|{address.strip().lower()}"


def get_watch_state(state: dict[str, Any], chat_id: int | str, address: str) -> dict[str, Any]:
    watch = state.setdefault("watch", {})
    key = watch_key(chat_id, address)
    ws = watch.get(key)
    if not isinstance(ws, dict):
        ws = {"seeded": False, "positions": {}, "fired": {}}
        watch[key] = ws
    ws.setdefault("positions", {})
    ws.setdefault("fired", {})
    return ws


def prune_watch_state(state: dict[str, Any], live_keys: set[str]) -> None:
    """Drop runtime state for (chat, address) pairs nobody subscribes to any
    more, so the state file cannot grow without bound."""
    watch = state.get("watch", {})
    for key in list(watch.keys()):
        if key not in live_keys:
            watch.pop(key, None)
