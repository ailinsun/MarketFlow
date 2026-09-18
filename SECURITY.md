# Security

## Reporting

Open an issue at
[github.com/ailinsun/MarketFlow/issues](https://github.com/ailinsun/MarketFlow/issues).
For anything that would expose keys or funds if described publicly, open an issue
saying only that you have a private report and asking for a contact channel.

Active development has ended; there is no guaranteed response time. Assume you are
reading and running this code on your own judgment.

## Threat model

This system holds no user funds and never takes custody of an account. What it has is
the ability to place orders on an account whose owner delegated exactly that. Those
threats, in order of how much they cost when they happen:

| Threat | Defence | Enforced by |
|---|---|---|
| Key exfiltration | the trading key lives in the account holder's enclave; this service holds only a side-bound agent credential, never a wallet or root key | `guardian.turnkey` |
| Unauthorised withdrawal | the agent's policy can express one order shape and no transfer | scoped enclave policy |
| A single compromised signer | the account holder's root quorum needs at least two signers, and the service is not a member of it | `guardian.authority`, with a test |
| An agent that outlives its mandate | the authority proof expires, and every arm and signature re-reads it | `guardian.authority`, `guardian.arm` |
| Runaway execution | arm state, caps, drawdown fuse, kill file | `execution.orders`, with tests |
| A mismatched or foreign mandate | authority proof checked against the encrypted tenant record (sub-org, signer, funder, agent key) | `guardian.executor` |
| Replayed or duplicated orders | idempotency key per intent | `execution.orders` |
| Acting on a market that will not settle cleanly | resolution gate, on-chain oracle read | `risk.resolution`, `monitor.settlement_guard` |
| A silently stopped money path | heartbeat freshness watchdogs | `risk.money_path`, `monitor.watchdog` |
| Analysis code gaining the ability to act | import boundary asserted from the syntax tree | `risk.exposure` self-test |

## Trust boundaries

Everything from a venue API or a chain read is **untrusted input**. A missing or
malformed field is a refusal, never a default that widens a limit. Fallbacks are
chosen so that failure tightens rather than loosens: an unknown fee rate may be
*estimated* for a dry run but **refuses a live taker order**; an unreadable arm state
resolves to *dry run*; an unavailable filter yields *no* filtering with the source
recorded, rather than silently passing everything.

The one-way arrow matters more than any individual check: analysis may feed execution,
execution may request authority, and nothing flows back. `marketflow.risk.exposure`
parses its own syntax tree and fails its self-test if it ever imports an execution
module.

## Before you connect a real wallet

1. **Verify the pinned SDK commit.** `requirements-signing.txt` pins the venue SDK to a
   specific commit rather than a branch, because a rebuild must not silently change
   code that produces signatures. Read that commit before trusting it with a key.
2. **Set the capital base and mandate tier yourself.** The shipped defaults are
   illustrative. `MARKETFLOW_CAPITAL_BASE_USD` and `MARKETFLOW_MANDATE_CAPITAL_USD`
   scale every limit in the system.
3. **Leave `MARKETFLOW_ENTRY_SOURCES` unset until you mean it.** Unset means no entry
   source is allow-listed and nothing is entered. Fail-closed is the default state, not
   an error state.
4. **Set `MARKETFLOW_RUNTIME`** to a directory you control and back up. Arm state,
   ledgers and caps live there.
5. **Confirm the kill file path** and that touching it stops execution, before you
   need it to.
6. **Run in dry run through a full settlement cycle.** Settlement is where the
   surprises are, and a dry run costs nothing.

## What this repository never contains

The publication gate (`checks/check_public.py`, run by `make check` in CI) fails the
build on: credentials and tokens, private keys, wallet addresses outside a reviewed
allowlist of public protocol contracts, Telegram or chat identifiers, private
hostnames and infrastructure IPs, local filesystem paths, unfilled placeholders,
imports of modules that are not part of the release, and personal identifiers.

The gate also carries **negative controls**: a fixture of deliberate violations that
must be detected, so a rule that quietly stops matching fails the suite rather than
passing silently. `gitleaks` is run as an independent second layer, because a pattern
scanner and a secret scanner miss different things.

Binary files are never scanned as text. Each is pinned to the digest it was reviewed
at, so a changed or added binary fails until a human looks at it.

## Deliberate non-features

- **No credential storage of any kind in this repository.** `.env.example` holds
  placeholders; `.env` is git-ignored.
- **No local private-key path.** The credential store refuses a wallet key, a root
  credential or an all-sides agent credential, and there is no second signing backend
  to fall back to: a tenant record that is not a user-root delegation cannot sign at
  all.
- **No automated entry without an allow-listed source.** Automation exits positions;
  entry requires an explicitly configured signal source.
- **No path by which the trading service can withdraw funds.** This is a property of
  the enclave policy, not of the code that calls it — which is the point. Code can be
  wrong; a policy that cannot express a transfer cannot be talked into one.
