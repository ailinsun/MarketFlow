# Configuration

Every setting is an environment variable. There is no configuration file to get out of
sync with the code, and nothing is read from a hard-coded path.

**The governing rule: unset means closed.** Every gate below defaults to the state that
refuses. An unconfigured system does nothing rather than everything.

## Where state is written

| Variable | Default | Effect |
|---|---|---|
| `MARKETFLOW_RUNTIME` | `<project>/runtime` | Root of all mutable state: ledgers, arm state, caches, heartbeats. `marketflow.paths` is the only module that resolves it, so setting it moves every artefact at once. Set this for any installed deployment; the default writes beside the source. |
| `MARKETFLOW_RISK_STATE_DIR` | a temporary directory | Where the read-only data plane keeps risk state. |
| `MARKETFLOW_GUARDIAN_ROOT` | under the runtime root | Mandate registry, per-mandate directories, the authority proofs and the audit trail. This is also where the guardian gate files live (below). |

## Risk limits

Nothing in this system is a dollar constant. Every cap is a fraction of one of these
bases, so a deployment changes its size by changing a base, not by editing code.

| Variable | Default | Effect |
|---|---|---|
| `MARKETFLOW_CAPITAL_BASE_USD` | `1000000` | Base for the operator execution path. Total deployment 20%, per trade 0.5%, drawdown fuse 2%; ceilings 100% / 5% / 10%. |
| `MARKETFLOW_MANDATE_CAPITAL_USD` | `1000000` | Base for delegated mandates. `standard` allows 50% / 2% / 10%; `professional` 100% / 5% / 20%. |
| `MARKETFLOW_FLEET_CAPITAL_USD` | `10000000` | Base for fleet-wide guards; the daily drawdown halt is 0.5% of it. |

**The shipped fractions are illustrative.** They are not an industry standard and not
advice. They exist so the system behaves coherently out of the box and so the tests can
assert scale invariance rather than dollar amounts.

## Hard gates

| Variable | Default | Effect when unset |
|---|---|---|
| `MARKETFLOW_ENTRY_SOURCES` | empty | **No signal source may open a position.** Comma-separated allow-list; empty is closed, not open. |
| `MARKETFLOW_MIN_ROOT_QUORUM` | `2` | Minimum signers on a root authority. Floored at 2 in code — a lower value is ignored, a higher one honoured. |
| `MARKETFLOW_POLYMARKET_KILL` | a path under the runtime root | Touch the file to halt execution. Confirm the path before you need it. |
| `MARKETFLOW_NIGHT_RULE_ENABLED` | `0` | An optional time-of-day trap rule, off by default. |

### Gate files

Some gates are files rather than variables, because their whole point is that they can
be closed without editing anything or restarting with a new environment. All of them
live under `MARKETFLOW_GUARDIAN_ROOT`; **absent means closed**, and nothing in the
service creates one.

| File | Open means |
|---|---|
| `GUARDIAN_LIVE_ENABLED` | this deployment may place live orders at all |
| `GUARDIAN_ENTRY_ENABLED` | automated BUY is permitted fleet-wide (exits are never gated by it) |
| `GUARDIAN_USER_ROOT_ENABLED` | user-root delegated mandates may be admitted |
| `GUARDIAN_TURNKEY_ENABLED` | mandates may sign through the enclave |

## Pluggable seams

Loads a dotted module path at runtime. **Unconfigured means deny.** A missing module
does not mean "everything is routed".

| Variable | Contract |
|---|---|
| `MARKETFLOW_INTEL_ROUTER` | module routing a market to its intelligence sources |

## Signing layer

Only read when placing real orders; see [SECURITY.md](../SECURITY.md).

| Variable | Effect |
|---|---|
| `MARKETFLOW_GUARDIAN_MASTER_KEY` | Fernet key for the mandate credential store at rest. |
| `MARKETFLOW_TURNKEY_API_BASE` | Enclave signing service endpoint; defaults to the provider's public API. |
| `MARKETFLOW_GUARDIAN_HTTP_HOST` / `_PORT` | Loopback control API bind address; defaults to `127.0.0.1:8790`. Do not bind it publicly. |
| `MARKETFLOW_GUARDIAN_HTTP_TOKEN_FILE` | Shared token that control-API callers must present. No token configured means every request is refused. |

## Operator alerts

Unconfigured, `marketflow.monitor.notify` drops the alert and returns `False`. It never
raises, because an alerting failure must not take down the thing it is watching — and
it never silently reports success either.

| Variable | Effect |
|---|---|
| `MARKETFLOW_ALERT_WEBHOOK` | POST destination for operator alerts. |
| `MARKETFLOW_ALERT_BOT_TOKEN` / `MARKETFLOW_ALERT_CHAT_ID` | Chat delivery, when a webhook is not in use. Both are required together. |

## Optional inputs and services

| Variable | Effect |
|---|---|
| `MARKETFLOW_WHALE_FEED` | Path to a locally collected large-print tape. Unset: the endpoint that reads it reports no feed configured rather than failing. |
| `MARKETFLOW_FEED_MAX_ACTIVE_BYTES` | Rotation threshold for active JSONL tapes; default 256 MiB. |
| `MARKETFLOW_POLYMARKET_PROXY_URL` | Egress proxy for venue traffic, when a deployment routes through one. Unset: connections are direct. |
| `MARKETFLOW_UMA_SUBGRAPH_BASE` | Settlement-history subgraph. Unset: that layer reports itself unavailable. |
| `MARKETFLOW_MCP_PORT` | Port for the read-only data plane; defaults to 8791. It binds loopback only, and nothing about it is a credential. |

## Checking what is in effect

```sh
python3 -c "
from marketflow import runtime_dir
from marketflow.execution import orders
print('runtime      ', runtime_dir())
print('capital base ', orders.REFERENCE_CAPITAL_USD)
print('per trade    ', orders.DEFAULT_MAX_PER_TRADE_USD)
print('ceiling      ', orders.OWNER_CAP_CEILING_PER_TRADE_USD)
"
```
