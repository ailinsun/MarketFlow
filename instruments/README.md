# Instruments

Standalone research methods, Python 3.9+, standard library only. Every script supports offline `--help`, and all but the snapshot verifier carry a synthetic `--selftest`; `make verify` runs them with network access denied by an audit hook.

These are the measurement half of the repository. The system built on their conclusions is in [`marketflow/`](../marketflow/); the two never import each other except where a single implementation is shared deliberately, which is noted below.

| File | Purpose and input |
|---|---|
| [fee_geometry.py](fee_geometry.py) | Compare fee curves and solve a fixed-distribution revenue-neutral scenario; uses embedded historical aggregate calibration, no external input. |
| [predmkt_zero_sum_ledger.py](predmkt_zero_sum_ledger.py) | Original fill-accounting method, with local `--tape`, `--meta` and `--fees`; defaults point into the untracked data input directory and fail clearly if absent. |
| [polymarket_farm_filter.py](polymarket_farm_filter.py) | Detect repeated-size signatures from `--path` feed JSONL or `--source tape` gzip input; CLI prints aggregates and never wallet lists. |
| [farm_rank_audit.py](farm_rank_audit.py) | Compare rank-cohort farm shares using local `--tape` and `--farm`; `--out` defaults into the untracked generated-data directory. |
| [uma_onchain.py](uma_onchain.py) | Decode public UMA proposal state; synthetic fixtures test the decoder offline. Explicit live-query arguments use public RPC endpoints. **The implementation lives in [`marketflow/monitor/uma_onchain.py`](../marketflow/monitor/uma_onchain.py)** and this file is its command-line face, so a number a report cites and a number the runtime acts on cannot drift apart. |
| [cross_venue_spread.py](cross_venue_spread.py) | One-shot Polymarket/Kalshi matched-pair spread scan with both venues' taker fees modelled. Matching is a token-similarity heuristic and does not verify that two contracts settle on the same event; every reported pair carries both sides' rules text for a human to check. |
| [maker_markout.py](maker_markout.py) | Post-fill markout for resting orders: adverse selection and the break-even spread. Reads a locally collected fills file. |
| [volume_census.py](volume_census.py) | Volume and participation census over a local market snapshot. |
| [kalshi_structure_census.py](kalshi_structure_census.py) | Contract-structure census of the other venue: how outcomes are enumerated and settled. |
| [uma_dispute_concentration.py](uma_dispute_concentration.py) | Concentration of dispute activity across proposers and markets in the settlement register. |
| [n_eff_estimator.py](n_eff_estimator.py) | Effective sample size of an autocorrelated series: Wolff (2003) automated windowing with a Sokal-style cross-check. No data dependency; known-answer tested against the AR(1) closed form. Use it before assuming a backtest's observations are independent. |
| [verify_snapshots.py](verify_snapshots.py) | Check published ledger closure, band totals, settlement groups, rank rates, exposure privacy and contract-pair structure; `--data` selects a snapshot directory. |

The raw tape, metadata caches and per-wallet lists are not included. Do not claim that fetching a new live window reproduces the historical snapshot. The ledger uses its original legacy-cache convention: metadata lacking a `closed` field is treated as coming from a closed-market cache; newly supplied metadata should explicitly include that field.

Farm feed fields: `wallet`, `wallet_label`, `condition_id`, `side`, `price`, `size`, `ts_source_ms` (or `ts_ingest_ms`). Tape fields: `w`, `cid`, `side`, `px`, `sz`, `ts`; ledger tapes also need `asset`. Such inputs are local research material and are excluded from publication.

Collectors, vendor benchmarks and private snapshot generators were not published. Their absence does not make historical report numbers reproducible; the [report index](../reports/README.md) says which reports rest on inputs that no longer exist.
