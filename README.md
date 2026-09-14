# MarketFlow

**Research on Polymarket prediction markets: order flow, market microstructure,
event-contract fees, repeated trading patterns, and UMA resolution.**

**Flagship paper:**  
*The Friction Ledger: A Zero-Sum Accounting of Polymarket Order Flow and the Geometry of Event-Contract Fees* — SSRN

**Sample:** 1,338,933 BUY-side fills · 439 settled markets · $419.4M buy notional

MarketFlow contains frozen aggregate snapshots, research instruments,
verification scripts, and working reports by Ailin Sun.

## The ledger

Window: **2025-12-11 to 2026-08-13**. Sample: **439 settled markets**, **1,338,933 taker BUY fills**, **$419.4M buy notional**, selected from the active core by large-print activity.

| Party | Net share of taker BUY notional |
|---|---:|
| Buying crowd, after modelled fees | −1.345% |
| Counterparties, after modelled fees | −0.143% |
| Venue, modelled taker fees | +1.488% |

The three displayed rows sum to zero. These are flow-accounting rows under the stated fee model, not observed participant profit-and-loss accounts. [Snapshot and sampling method](data/zero_sum_ledger/zero_sum_ledger.json).

- **Fee geometry:** the published floor scenario reduces mid-range fees by **25.9%** under a fixed calibrated distribution; this is a model calculation, not an observed reform outcome. [Arithmetic](instruments/fee_geometry.py).
- **Farm signature:** **46.2%** of the highest dollar-volume rank cohort meets the repeated-size pattern. The signature describes behaviour, not common control or intent. [Rank audit and cohort definition](data/farm_signature/rank_audit_2026-08-13.json).
- **Settlement register:** **7.12%** of settled disputes changed the proposal. This is a historical conditional rate, not a forecast for an individual contract. [Register](data/settlement_quality/settlement_quality.json).

## Reproduce

Python 3.9 or newer; the retained code uses only the standard library. From the repository root:

```sh
python3 -B instruments/fee_geometry.py
python3 -B instruments/verify_snapshots.py
make test
```

These commands recalculate the fee table, verify aggregate identities and run offline self-tests. They do not recreate missing raw trading histories. `make check` scans the public files; [instrument inputs](instruments/README.md) document how to use independently supplied local data.

## Data

[File inventory, windows, provenance and citation blocks](data/README.md): [ledger](data/zero_sum_ledger/), [settlement](data/settlement_quality/), [farm aggregates](data/farm_signature/), [contract pairs](data/contract_equivalence/), [schemas](data/schemas/), [exposure comparison](data/whale_exposure/).

Hugging Face data mirrors: [ledger](https://huggingface.co/datasets/ailinsun/polymarket-zero-sum-ledger), [settlement register](https://huggingface.co/datasets/ailinsun/polymarket-settlement-quality-register), [contract-pair annotations](https://huggingface.co/datasets/ailinsun/polymarket-kalshi-contract-equivalence-gold).

Trader addresses, usernames and wallet lists are omitted. Contract-pair labels remain at their original `initial` review status. [Report index](reports/README.md) distinguishes surviving evidence from unavailable raw inputs.

## Limitations

- The selected active-market tape is not a census of the venue. ([Whale report](reports/en/260726_polymarket-whale-behavior-report.md))
- Capped trade histories retain recent fills and omit older activity. ([Whale report](reports/en/260726_polymarket-whale-behavior-report.md))
- Resolved-only analysis underrepresents long-dated, unsettled markets. ([Whale report](reports/en/260726_polymarket-whale-behavior-report.md))
- Fills in the same market share an outcome; per-print observations are correlated. ([Whale report](reports/en/260726_polymarket-whale-behavior-report.md))
- Repeated trading patterns do not establish identity, control, intent or future performance. ([Clean-data method](reports/zh/260812_clean-data-method.md))
- Historical raw artifacts were not retained or are not distributed; aggregate checks cannot reproduce every original report table. ([Whale report](reports/en/260726_polymarket-whale-behavior-report.md))

## Citation

See [CITATION.cff](CITATION.cff). Archived release **v0.1.2**: [DOI 10.5281/zenodo.22734802](https://doi.org/10.5281/zenodo.22734802).

## Licence

Code: [Apache-2.0](LICENSE). Data, reports and research documentation: [CC BY 4.0](LICENSE-DATA). Upstream venue material retains its own terms and rights.

## Author

Ailin Sun · [LinkedIn](https://www.linkedin.com/in/ailinsun/).
