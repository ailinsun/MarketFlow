"""Scoped proxy helpers for the local Polymarket SDK integration.

The Polymarket SDK constructs its own httpx clients internally and does not
expose a public proxy argument. These helpers patch only the SDK transport
factory while a SecureClient is being created, so process-wide proxy
environment variables do not leak into unrelated feeds or LLM calls.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlparse, urlunparse


POLYMARKET_PROXY_URL_ENV = "MARKETFLOW_POLYMARKET_PROXY_URL"
POLYMARKET_PROXY_LEGACY_ENV = "POLYMARKET_HTTP_PROXY"
POLYMARKET_PROXY_SECRET_REF = "polymarket_proxy_url.txt"
# No proxy unless the deployment configures one: venue traffic goes direct.
DEFAULT_POLYMARKET_PROXY_URL = ""
# Both versions have the byte-for-byte same transport implementation and the
# exact constructor surface patched below.  b21 is the locked live runtime; b8
# remains supported for the audited validation environment.  Unknown versions
# still fail closed before any client is created.
SUPPORTED_POLYMARKET_SDK_VERSIONS = frozenset({"0.1.0b8", "0.1.0b21"})
POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"

_OFF_VALUES = {"", "0", "false", "no", "none", "off", "direct"}
_SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks5h"}
_PATCH_LOCK = threading.RLock()


class PolymarketProxyError(Exception):
    """Raised when a Polymarket SDK proxy configuration is invalid."""


def normalize_polymarket_proxy_url(value: str | None) -> str | None:
    raw = str(value or "").strip().replace("\\n", "").replace("\\r", "")
    if raw.lower() in _OFF_VALUES:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _SUPPORTED_SCHEMES:
        raise PolymarketProxyError(
            f"{POLYMARKET_PROXY_URL_ENV}/{POLYMARKET_PROXY_LEGACY_ENV} must use one of: "
            f"{', '.join(sorted(_SUPPORTED_SCHEMES))}"
        )
    if not parsed.hostname:
        raise PolymarketProxyError(
            f"{POLYMARKET_PROXY_URL_ENV}/{POLYMARKET_PROXY_LEGACY_ENV} must include a proxy host"
        )
    return raw


def _polymarket_sdk_proxy_raw_config(secret_dir: str | None = None) -> tuple[str | None, str]:
    raw = os.environ.get(POLYMARKET_PROXY_URL_ENV)
    if raw is not None:
        return raw, f"env:{POLYMARKET_PROXY_URL_ENV}"
    raw = os.environ.get(POLYMARKET_PROXY_LEGACY_ENV)
    if raw is not None:
        return raw, f"env:{POLYMARKET_PROXY_LEGACY_ENV}"
    if secret_dir:
        path = os.path.join(secret_dir, POLYMARKET_PROXY_SECRET_REF)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip(), f"secret:{POLYMARKET_PROXY_SECRET_REF}"
    return DEFAULT_POLYMARKET_PROXY_URL, "default"


def polymarket_sdk_proxy_url(secret_dir: str | None = None) -> str | None:
    raw, _source = _polymarket_sdk_proxy_raw_config(secret_dir)
    return normalize_polymarket_proxy_url(raw)


def redacted_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username or parsed.password:
        host = f"***@{host}"
    return urlunparse((parsed.scheme, host, "", "", "", ""))


def polymarket_sdk_version_summary() -> dict[str, object]:
    try:
        import polymarket

        current = getattr(polymarket, "__version__", None)
        return {
            "expected": sorted(SUPPORTED_POLYMARKET_SDK_VERSIONS),
            "current": current,
            "ok": current in SUPPORTED_POLYMARKET_SDK_VERSIONS,
        }
    except Exception as exc:
        return {
            "expected": sorted(SUPPORTED_POLYMARKET_SDK_VERSIONS),
            "current": None,
            "ok": False,
            "error_type": type(exc).__name__,
            "error": _short_error(exc),
        }


def assert_polymarket_sdk_version() -> None:
    info = polymarket_sdk_version_summary()
    if not info.get("ok"):
        raise PolymarketProxyError(
            "Unsupported polymarket-client SDK version for scoped proxy patch: "
            f"expected {info.get('expected')}, got {info.get('current')}"
        )
    # Version is necessary but not sufficient: verify the private constructor
    # seam itself before monkey-patching it.  A repacked or backported wheel with
    # the same version string but a changed transport API must also fail closed.
    import inspect
    from polymarket.clients._transport import AsyncTransport, SyncTransport

    expected = ("base_url", "options", "logger", "client", "header_resolver")
    for transport in (SyncTransport, AsyncTransport):
        actual = tuple(inspect.signature(transport).parameters)
        if actual != expected:
            raise PolymarketProxyError(
                "Polymarket SDK transport constructor changed; refusing scoped proxy patch "
                f"({transport.__name__}: {actual!r})"
            )


def polymarket_sdk_proxy_summary(secret_dir: str | None = None) -> dict[str, object]:
    _raw, source = _polymarket_sdk_proxy_raw_config(secret_dir)
    proxy_url = polymarket_sdk_proxy_url(secret_dir)
    return {
        "enabled": bool(proxy_url),
        "scope": "polymarket_sdk_httpx_only",
        "trust_env": False,
        "env_var": POLYMARKET_PROXY_URL_ENV,
        "compat_env_var": POLYMARKET_PROXY_LEGACY_ENV,
        "secret_ref": POLYMARKET_PROXY_SECRET_REF,
        "source": source if source in {"default", f"secret:{POLYMARKET_PROXY_SECRET_REF}"} else "env",
        "sdk_version": polymarket_sdk_version_summary(),
    }


def polymarket_sdk_proxy_probe(
    secret_dir: str | None = None,
    *,
    timeout: float = 4.0,
) -> dict[str, object]:
    """Check what Polymarket sees through the configured scoped SDK proxy.

    This is a diagnostics helper only. It never opens a SecureClient, never signs
    anything, never places an order, and never falls back to direct traffic.
    """
    started = time.perf_counter()
    probe: dict[str, object] = {
        "checked": True,
        "target": POLYMARKET_GEOBLOCK_URL,
        "ok": False,
        "blocked": None,
        "trust_env": False,
    }
    try:
        proxy_url = polymarket_sdk_proxy_url(secret_dir)
        if not proxy_url:
            probe.update({"checked": False, "status": "direct_disabled", "reason": "proxy disabled"})
            return probe

        import httpx

        with httpx.Client(proxy=proxy_url, timeout=timeout, trust_env=False) as client:
            response = client.get(POLYMARKET_GEOBLOCK_URL)
            probe["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            probe.update({"status": "invalid_response", "error": "geoblock response is not a JSON object"})
            return probe

        country = data.get("country")
        region = data.get("region")
        blocked = data.get("blocked")
        exit_ok = blocked is not True
        probe.update(
            {
                "status": "exit_ok" if exit_ok else "blocked",
                "ok": exit_ok,
                "country": country,
                "region": region,
                "blocked": blocked,
            }
        )
        return probe
    except Exception as exc:
        probe.update(
            {
                "status": "probe_failed",
                "error_type": type(exc).__name__,
                "error": _short_error(exc),
            }
        )
        return probe
    finally:
        probe["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)


def polymarket_sdk_proxy_status(
    secret_dir: str | None = None,
    *,
    probe: bool = False,
    timeout: float = 4.0,
) -> dict[str, object]:
    status = polymarket_sdk_proxy_summary(secret_dir)
    if probe:
        status["probe"] = polymarket_sdk_proxy_probe(secret_dir, timeout=timeout)
    return status


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:240] if text else type(exc).__name__


# A TLS path that runs through an SSH tunnel and a forward proxy goes half-dead
# intermittently: the tunnel hits an idle timeout, or the far end stalls mid
# handshake. With the SDK's default http2=True, a keepalive connection the peer
# has already FINed (local socket sitting in CLOSE_WAIT) is still handed back out
# of the pool as idle, and the next request does a TLS read on that dead
# connection and hangs in select forever. Observed stacks:
# _ssl__SSLSocket_do_handshake_impl / _ssl__SSLSocket_read / PySSL_select.
#
# So every httpx transport inside the venue client is hardened:
#   (1) an explicit four-dimension timeout — connect/read/write/pool. Whichever
#       one stalls raises httpx.Timeout in bounded time, letting a daemon tick
#       fail cleanly and retry on the next one instead of hanging forever;
#   (2) http2=False, which removes the whole class of half-dead h2-over-proxy
#       keepalive hangs. The venue's REST surfaces all work over HTTP/1.1; h2 is
#       only the SDK's default, not a requirement;
#   (3) a short keepalive_expiry and a small pool ceiling, so idle connections
#       are retired rather than reused after a tunnel has killed them.
# connect=8 / read=15 leaves room for a slow tunnel while staying a hard bound;
# the SDK's own 5/10 is tight and does not cover a tunnel handshake stall.
_TUNNEL_CONNECT_TIMEOUT = 8.0
_TUNNEL_READ_TIMEOUT = 15.0
_TUNNEL_WRITE_TIMEOUT = 15.0
_TUNNEL_POOL_TIMEOUT = 5.0
_TUNNEL_KEEPALIVE_EXPIRY = 15.0
_TUNNEL_MAX_CONNECTIONS = 10
_TUNNEL_MAX_KEEPALIVE = 2


def _tunnel_hardened_client_kwargs(httpx_mod: object) -> dict[str, object]:
    """The hardened httpx.Client / AsyncClient keyword arguments, in one place."""
    return {
        "timeout": httpx_mod.Timeout(
            connect=_TUNNEL_CONNECT_TIMEOUT,
            read=_TUNNEL_READ_TIMEOUT,
            write=_TUNNEL_WRITE_TIMEOUT,
            pool=_TUNNEL_POOL_TIMEOUT,
        ),
        "limits": httpx_mod.Limits(
            max_connections=_TUNNEL_MAX_CONNECTIONS,
            max_keepalive_connections=_TUNNEL_MAX_KEEPALIVE,
            keepalive_expiry=_TUNNEL_KEEPALIVE_EXPIRY,
        ),
        "http2": False,
    }


def ensure_sdk_tick_size_whitelist() -> None:
    """Some SDK builds ship a tick-size allowlist that omits 0.0025. When the
    venue returns minimum_tick_size=0.0025 for a batch of markets, such an SDK
    raises outright and the whole batch becomes untradeable — an entry signal
    builds fine and the execution layer refuses it.

    This adds 0.0025 with the same RoundingConfig precision as 0.0001
    (amount=6 / price=4 / size=2, following the existing amount=price+2 pattern).
    Idempotent, and applied the same way as the httpx hardening: the patch lives
    in this repository rather than in site-packages, so an SDK upgrade cannot
    silently drop it."""
    from decimal import Decimal
    from polymarket._internal.actions.orders import context as _ctx
    from polymarket._internal.actions.orders import market_data as _md

    t = Decimal("0.0025")
    if t not in _md._ALLOWED_TICK_SIZES:
        _md._ALLOWED_TICK_SIZES = frozenset(_md._ALLOWED_TICK_SIZES | {t})
    if t not in _ctx._ROUNDING_BY_TICK:
        _ctx._ROUNDING_BY_TICK[t] = _ctx.RoundingConfig(amount=6, price=4, size=2)


@contextlib.contextmanager
def proxied_secure_client_transports(proxy_url: str | None) -> Iterator[None]:
    """Patch synchronous SecureClient transports for one construction window."""
    proxy_url = normalize_polymarket_proxy_url(proxy_url)
    assert_polymarket_sdk_version()
    ensure_sdk_tick_size_whitelist()

    import httpx
    import polymarket.clients.secure as secure_module
    from polymarket.clients._transport import TransportOptions

    with _PATCH_LOCK:
        original = secure_module.SyncTransport

        def sync_transport_with_proxy(
            *,
            base_url: str,
            options: object | None = None,
            logger: object | None = None,
            client: object | None = None,
            header_resolver: object | None = None,
        ) -> object:
            if client is None:
                opts = options or TransportOptions()
                client = httpx.Client(
                    base_url=base_url,
                    event_hooks=dict(opts.event_hooks) if opts.event_hooks else None,
                    proxy=proxy_url,
                    trust_env=False,
                    **_tunnel_hardened_client_kwargs(httpx),
                )
                transport = original(
                    base_url=base_url,
                    options=options,
                    logger=logger,
                    client=client,
                    header_resolver=header_resolver,
                )
                transport._owns_client = True
                return transport
            return original(
                base_url=base_url,
                options=options,
                logger=logger,
                client=client,
                header_resolver=header_resolver,
            )

        secure_module.SyncTransport = sync_transport_with_proxy
        try:
            yield
        finally:
            secure_module.SyncTransport = original


@contextlib.contextmanager
def proxied_async_secure_client_transports(proxy_url: str | None) -> Iterator[None]:
    """Patch asynchronous SecureClient transports for one construction window."""
    proxy_url = normalize_polymarket_proxy_url(proxy_url)
    assert_polymarket_sdk_version()
    ensure_sdk_tick_size_whitelist()

    import httpx
    import polymarket.clients.async_secure as async_secure_module
    from polymarket.clients._transport import TransportOptions

    with _PATCH_LOCK:
        original = async_secure_module.AsyncTransport

        def async_transport_with_proxy(
            *,
            base_url: str,
            options: object | None = None,
            logger: object | None = None,
            client: object | None = None,
            header_resolver: object | None = None,
        ) -> object:
            if client is None:
                opts = options or TransportOptions()
                client = httpx.AsyncClient(
                    base_url=base_url,
                    event_hooks=dict(opts.event_hooks) if opts.event_hooks else None,
                    proxy=proxy_url,
                    trust_env=False,
                    **_tunnel_hardened_client_kwargs(httpx),
                )
                transport = original(
                    base_url=base_url,
                    options=options,
                    logger=logger,
                    client=client,
                    header_resolver=header_resolver,
                )
                transport._owns_client = True
                return transport
            return original(
                base_url=base_url,
                options=options,
                logger=logger,
                client=client,
                header_resolver=header_resolver,
            )

        async_secure_module.AsyncTransport = async_transport_with_proxy
        try:
            yield
        finally:
            async_secure_module.AsyncTransport = original
