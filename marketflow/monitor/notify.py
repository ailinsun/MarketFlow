#!/usr/bin/env python3
"""Operator alert sink — one place every watchdog and guard delivers through.

There are no destinations baked into this file. A deployment chooses one by
environment variable; with nothing configured, `send_alert` returns False and
the caller degrades honestly (a watchdog that cannot reach anyone must say so,
never pretend it delivered).

    MARKETFLOW_ALERT_WEBHOOK      any HTTPS endpoint; receives {"text": ...} JSON
    MARKETFLOW_ALERT_BOT_TOKEN    Telegram bot token, paired with
    MARKETFLOW_ALERT_CHAT_ID      the chat that should receive operator alerts

Both may be set; each configured channel is attempted and `send_alert` is True
when at least one accepted the message. Secrets are read from the environment
only, never from a file in this repository, and are never echoed into the
return value, logs or exceptions.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT_SEC = 10.0


def _post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> bool:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def webhook_configured() -> bool:
    return bool(os.environ.get("MARKETFLOW_ALERT_WEBHOOK"))


def telegram_configured() -> bool:
    return bool(os.environ.get("MARKETFLOW_ALERT_BOT_TOKEN")
                and os.environ.get("MARKETFLOW_ALERT_CHAT_ID"))


def configured() -> bool:
    """True when at least one delivery channel is available."""
    return webhook_configured() or telegram_configured()


def send_alert(text: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> bool:
    """Deliver an operator alert. Returns True only if a channel accepted it."""
    if not text:
        return False
    delivered = False

    url = os.environ.get("MARKETFLOW_ALERT_WEBHOOK")
    if url:
        delivered |= _post(
            url,
            json.dumps({"text": text}).encode("utf-8"),
            {"Content-Type": "application/json"},
            timeout,
        )

    token = os.environ.get("MARKETFLOW_ALERT_BOT_TOKEN")
    chat = os.environ.get("MARKETFLOW_ALERT_CHAT_ID")
    if token and chat:
        delivered |= _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8"),
            {"Content-Type": "application/x-www-form-urlencoded"},
            timeout,
        )

    return delivered


def selftest() -> int:
    """Offline checks: no network, no configuration required."""
    saved = {k: os.environ.pop(k, None) for k in
             ("MARKETFLOW_ALERT_WEBHOOK", "MARKETFLOW_ALERT_BOT_TOKEN", "MARKETFLOW_ALERT_CHAT_ID")}
    try:
        assert configured() is False, "no channel should be configured"
        assert send_alert("x") is False, "unconfigured sink must report failure"
        assert send_alert("") is False, "empty text is never delivered"
        os.environ["MARKETFLOW_ALERT_BOT_TOKEN"] = "t"
        assert telegram_configured() is False, "token alone is not a channel"
        os.environ["MARKETFLOW_ALERT_CHAT_ID"] = "c"
        assert telegram_configured() is True and configured() is True
    finally:
        os.environ.pop("MARKETFLOW_ALERT_BOT_TOKEN", None)
        os.environ.pop("MARKETFLOW_ALERT_CHAT_ID", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("marketflow.monitor.notify selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
