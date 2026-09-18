# Quickstart

Everything here runs offline. No account, no credentials, no network.

## Verify and demonstrate

```sh
git clone https://github.com/ailinsun/MarketFlow && cd MarketFlow
make verify
make demo
```

Python 3.9 or newer, nothing installed. `make verify` runs the publication gate and
every self-test in the repository; `make demo` walks a synthetic portfolio through the
real risk and execution modules. (The optional signing install below needs 3.10+.)

`make verify` prints one line per check and ends non-zero on any failure. One line
says `SKIP ... needs cryptography` — that is the guardian suite, which needs the
optional install below. Everything else runs on the standard library.

## What `make verify` actually checks

| Step | What it proves |
|---|---|
| `checks/check_public.py` | no identifiers, secrets, private paths, broken links or undeclared imports |
| help entry points | every script starts and parses arguments without a network call |
| instrument self-tests | the research tools reproduce their own known answers |
| frozen aggregate verification | the published snapshots' accounting identities still close |
| package self-tests | every module in `marketflow` passes its own invariants |
| guardian suite | authority proofs, arm state, risk budget and the enclave signing layer (needs the signing install) |
| unit tests | rotation and risk-primitive regressions |
| the demonstration | the end-to-end path runs and produces the documented numbers |

A network attempt during any of it is a failure, not a warning. The runner installs an
audit hook that records a socket or urllib call **even when the code under test catches
the exception**, because several reads in this system are deliberately fail-soft and a
fail-soft read would otherwise hide a live dependency.

## Install as a package

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

```python
from marketflow.risk import exposure

positions = exposure.load_generic("examples/data/portfolio.json")
report = exposure.portfolio_exposure(positions, exposure.resolve_meta(positions))
print(report["gross_cost_usd"], report["true_event_exposure_usd"])
```

## Run individual pieces

Every module with an offline self-test can be run directly:

```sh
python3 -m marketflow.risk.exposure --selftest
python3 -m marketflow.risk.complete_sets --selftest
python3 -m marketflow.execution.market_gate --selftest
python3 -B instruments/fee_geometry.py              # recompute the fee table
python3 -B instruments/n_eff_estimator.py --selftest # effective sample size
python3 -B instruments/verify_snapshots.py          # check the frozen aggregates
```

## Use your own portfolio

`examples/data/portfolio.json` is synthetic, and its format is the one the multi-tenant
layer and the venue's public position API both produce. Point the exposure engine at
your own file of the same shape:

```sh
python3 -m marketflow.risk.exposure --source generic --positions my_positions.json
```

Required per row: `condition_id`, `outcome` (Yes/No), `shares`, and either `cost_usd`
or `entry_price`. Supply `event_slug` and `negative_risk` when you have them — without
them every leg becomes its own bucket and exposure falls back to the sum of costs,
which is the conservative direction but throws away the whole point of the engine.

## Enable the signing layer

Only needed to place real orders. Read [SECURITY.md](../SECURITY.md) first.

```sh
.venv/bin/pip install -r requirements-signing.txt
cp .env.example .env
```

Then set, at minimum, `MARKETFLOW_RUNTIME`, `MARKETFLOW_CAPITAL_BASE_USD` and
`MARKETFLOW_ENTRY_SOURCES`. Every variable is documented in
[configuration.md](configuration.md), including what each one does when unset.

Dry run is the default and stays the default until arm state says otherwise. A live
request without armed state resolves to dry run rather than failing, because refusing
is the safe direction.
