# Instruments

Standalone research methods, Python 3.9+, standard library only. Every script supports offline `--help`. All except the snapshot verifier have a synthetic `--selftest`; `make test` runs those tests with network access denied.

| File | Purpose and input |
|---|---|
| [fee_geometry.py](fee_geometry.py) | Compare fee curves and solve a fixed-distribution revenue-neutral scenario; uses embedded historical aggregate calibration, no external input. |
| [predmkt_zero_sum_ledger.py](predmkt_zero_sum_ledger.py) | Original fill-accounting method, with local `--tape`, `--meta` and `--fees`; defaults point into the untracked data input directory and fail clearly if absent. |
| [polymarket_farm_filter.py](polymarket_farm_filter.py) | Detect repeated-size signatures from `--path` feed JSONL or `--source tape` gzip input; CLI prints aggregates and never wallet lists. |
| [farm_rank_audit.py](farm_rank_audit.py) | Compare rank-cohort farm shares using local `--tape` and `--farm`; `--out` defaults into the untracked generated-data directory. |
| [uma_onchain.py](uma_onchain.py) | Decode public UMA proposal state; synthetic fixtures test the decoder offline. Explicit live-query arguments use public RPC endpoints. |
| [verify_snapshots.py](verify_snapshots.py) | Check published ledger closure, band totals, settlement groups, rank rates, exposure privacy and contract-pair structure; `--data` selects a snapshot directory. |

The raw tape, metadata caches and per-wallet lists are not included. Do not claim that fetching a new live window reproduces the historical snapshot. The ledger uses its original legacy-cache convention: metadata lacking a `closed` field is treated as coming from a closed-market cache; newly supplied metadata should explicitly include that field.

Farm feed fields: `wallet`, `wallet_label`, `condition_id`, `side`, `price`, `size`, `ts_source_ms` (or `ts_ingest_ms`). Tape fields: `w`, `cid`, `side`, `px`, `sz`, `ts`; ledger tapes also need `asset`. Such inputs are local research material and are excluded from publication.

Removed daemon collectors, vendor benchmarks and private snapshot generators are documented in [BUILD_REPORT.md](../BUILD_REPORT.md). Their removal does not make historical report numbers reproducible; see the [report index](../reports/README.md).
