# Data

Frozen research snapshots and schemas; filenames and all numerical JSON values are preserved. Only identifying infrastructure strings and unavailable internal references were scrubbed. The filename hash is the original method/content version, not a checksum of the redacted file bytes.

No raw trading tape, wallet lists or collector caches are distributed. The whale exposure table keeps its numerical rows with address and label fields omitted. The clean-ranking release contains aggregates without either per-wallet list.

All rows below use **CC BY 4.0** for the original compilation/annotations; upstream venue material retains its applicable rights. A file with an embedded citation block should be cited with that complete block, copied below. Otherwise cite Ailin Sun, this repository release and the exact filename; no missing citation block has been invented.

| File | What it is | Window or observation date | Source | Licence | Citation |
|---|---|---|---|---|---|
| [README.md](README.md) | File inventory, citation policy and replay limitations | Not a time-series observation | Publication review | CC BY 4.0 | Repository release + filename |
| [contract_equivalence/README.md](contract_equivalence/README.md) | Pair-label status and provenance guide | Labels dated 2026-08-24 | Publication review | CC BY 4.0 | Repository release + filename |
| [contract_equivalence/gold_pair.schema.json](contract_equivalence/gold_pair.schema.json) | Schema for initial pair annotations | Labelled 2026-08-24; schema has no observation window | Public Polymarket and Kalshi venue rules; AI-assisted annotations | CC BY 4.0 | Repository release + filename |
| [contract_equivalence/gold_pairs.jsonl](contract_equivalence/gold_pairs.jsonl) | Initial annotated contract pairs | Labelled 2026-08-24; schema has no observation window | Public Polymarket and Kalshi venue rules; AI-assisted annotations | CC BY 4.0 | Repository release + filename |
| [farm_signature/clean_data_2026-09-01_aggregates.json](farm_signature/clean_data_2026-09-01_aggregates.json) | Farm-filtered ranking aggregates | 2026-07-06 to 2026-09-01 | Polymarket public trade feed | CC BY 4.0 | [Original block](#citation-1) |
| [farm_signature/rank_audit_2026-08-13.json](farm_signature/rank_audit_2026-08-13.json) | Farm-share comparison by ranking key | 2025-12-11 to 2026-08-13 | Polymarket public trade feed | CC BY 4.0 | Repository release + filename |
| [schemas/canonical-contract.schema.json](schemas/canonical-contract.schema.json) | Canonical event-contract representation | Not a time-series observation | Experimental research schema | CC BY 4.0 | Repository release + filename |
| [schemas/equivalence-edge.schema.json](schemas/equivalence-edge.schema.json) | Directional relationship between contracts | Not a time-series observation | Experimental research schema | CC BY 4.0 | Repository release + filename |
| [schemas/policy.schema.json](schemas/policy.schema.json) | Research policy constraint vocabulary | Not a time-series observation | Experimental research schema | CC BY 4.0 | Repository release + filename |
| [settlement_quality/2026-08-13-9b25803e6ca8.json](settlement_quality/2026-08-13-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-2) |
| [settlement_quality/2026-08-14-9b25803e6ca8.json](settlement_quality/2026-08-14-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-3) |
| [settlement_quality/2026-08-15-9b25803e6ca8.json](settlement_quality/2026-08-15-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-4) |
| [settlement_quality/2026-08-16-9b25803e6ca8.json](settlement_quality/2026-08-16-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-5) |
| [settlement_quality/2026-08-17-9b25803e6ca8.json](settlement_quality/2026-08-17-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-6) |
| [settlement_quality/2026-08-18-9b25803e6ca8.json](settlement_quality/2026-08-18-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-7) |
| [settlement_quality/2026-08-19-9b25803e6ca8.json](settlement_quality/2026-08-19-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-8) |
| [settlement_quality/2026-08-20-9b25803e6ca8.json](settlement_quality/2026-08-20-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-9) |
| [settlement_quality/2026-08-21-9b25803e6ca8.json](settlement_quality/2026-08-21-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-10) |
| [settlement_quality/2026-08-22-9b25803e6ca8.json](settlement_quality/2026-08-22-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-11) |
| [settlement_quality/2026-08-23-9b25803e6ca8.json](settlement_quality/2026-08-23-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-12) |
| [settlement_quality/2026-08-24-9b25803e6ca8.json](settlement_quality/2026-08-24-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-13) |
| [settlement_quality/2026-08-25-9b25803e6ca8.json](settlement_quality/2026-08-25-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-14) |
| [settlement_quality/2026-08-26-9b25803e6ca8.json](settlement_quality/2026-08-26-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-15) |
| [settlement_quality/2026-08-27-9b25803e6ca8.json](settlement_quality/2026-08-27-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-16) |
| [settlement_quality/2026-08-28-9b25803e6ca8.json](settlement_quality/2026-08-28-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-17) |
| [settlement_quality/2026-08-29-9b25803e6ca8.json](settlement_quality/2026-08-29-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-18) |
| [settlement_quality/2026-08-30-9b25803e6ca8.json](settlement_quality/2026-08-30-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-19) |
| [settlement_quality/2026-08-31-9b25803e6ca8.json](settlement_quality/2026-08-31-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-20) |
| [settlement_quality/2026-09-01-9b25803e6ca8.json](settlement_quality/2026-09-01-9b25803e6ca8.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-21) |
| [settlement_quality/settlement_quality.json](settlement_quality/settlement_quality.json) | Settlement register snapshot; generated dates differ, source window is fixed | 2023-12-05 to 2026-08-12 | UMA Polygon Optimistic Oracle V2 public subgraph | CC BY 4.0 | [Original block](#citation-22) |
| [whale_exposure/README_zh.md](whale_exposure/README_zh.md) | Original explanation of the exposure correction | Export named 2026-08-13; embedded generation 2026-08-04; collection endpoints unspecified | Polymarket activity and settlement metadata | CC BY 4.0 | Repository release + filename |
| [whale_exposure/whale_exposure_v1v2_comparison_2026-08-13.json](whale_exposure/whale_exposure_v1v2_comparison_2026-08-13.json) | Exposure close-rule comparison; numerical rows only | Export named 2026-08-13; embedded generation 2026-08-04; collection endpoints unspecified | Polymarket activity and settlement metadata | CC BY 4.0 | Repository release + filename |
| [zero_sum_ledger/2026-08-13-858973c2494c.json](zero_sum_ledger/2026-08-13-858973c2494c.json) | Three-party ledger and band summaries | 2025-12-11 to 2026-08-13 | Polymarket public trade feed and market metadata | CC BY 4.0 | [Original block](#citation-23) |
| [zero_sum_ledger/ledger_raw_2026-08-13.json](zero_sum_ledger/ledger_raw_2026-08-13.json) | Raw aggregate reconciliation (not the raw tape) | 2025-12-11 to 2026-08-13 | Polymarket public trade feed and market metadata | CC BY 4.0 | Repository release + filename |
| [zero_sum_ledger/zero_sum_ledger.json](zero_sum_ledger/zero_sum_ledger.json) | Three-party ledger and band summaries | 2025-12-11 to 2026-08-13 | Polymarket public trade feed and market metadata | CC BY 4.0 | [Original block](#citation-24) |

## Public replay boundary

`python3 instruments/verify_snapshots.py` checks ledger dollar closure, rounding tolerance, band counts, settlement group totals, farm rank rates, anonymized exposure structure and pair fields. It does not regenerate an unavailable raw tape or independently re-audit label truth.

The original snapshot generators read intermediate outputs that are not distributed. Their method is preserved in `how_to_replay`, `method` and `rule_feature_definitions`: reconcile BUY fills against settled outcome prices and observed fee rates; compare the same wallet cohort under different rank keys; group UMA requests and compare proposed versus settled answers. The retained ledger and farm instruments accept independently supplied local inputs.

The ledger contains an original metadata inconsistency: `method.truncation` says “earliest first”, while `how_to_replay.caveats` and the collector notes say newest-first. Those original strings and all numbers are preserved; the public README follows the explicit newest-first observation. Settlement files with different generated dates can describe the same source window; do not treat them as independent samples.

The embedded citation blocks below point to the public repository and its archived release. Cite the versioned snapshot and filename, rather than treating a rolling pointer as a new observation.

## Protocol address allowlist

These are contract/requester adapters, not trader wallets. They appear in settlement `coverage.canonical_requesters`:

- `0x65070be91477460d8a7aeeb94ef92fe056c2f2a7` — Polymarket UMA CTF adapter.
- `0x69c47de9d4d3dad79590d61b9e05918e03775f24` — Polymarket NegRisk UMA CTF adapter.
- `0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d` — Polymarket NegRisk CTF adapter.

## Original citation blocks

<a id="citation-1"></a>
<details><summary>farm_signature/clean_data_2026-09-01_aggregates.json</summary>

```json
{
  "dataset": "adjusted-ranking-snapshot",
  "how_to_cite": "Ailin Sun. \"Polymarket wallet ranking, farm-filtered.\" Version 04fd708f9336 (2026-09-01), window 2026-07-06 to 2026-09-01. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/farm_signature/clean_data_2026-09-01_aggregates.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/farm_signature/clean_data_2026-09-01_aggregates.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/farm_signature/clean_data_2026-09-01_aggregates.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "clean-data-v0.2",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket wallet ranking, farm-filtered",
  "version": "04fd708f9336",
  "version_date": "2026-09-01"
}
```

</details>

<a id="citation-2"></a>
<details><summary>settlement_quality/2026-08-13-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-13), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-13-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-13-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-13"
}
```

</details>

<a id="citation-3"></a>
<details><summary>settlement_quality/2026-08-14-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-14), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-14-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-14-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-14"
}
```

</details>

<a id="citation-4"></a>
<details><summary>settlement_quality/2026-08-15-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-15), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-15-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-15-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-15"
}
```

</details>

<a id="citation-5"></a>
<details><summary>settlement_quality/2026-08-16-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-16), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-16-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-16-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-16"
}
```

</details>

<a id="citation-6"></a>
<details><summary>settlement_quality/2026-08-17-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-17), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-17-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-17-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-17"
}
```

</details>

<a id="citation-7"></a>
<details><summary>settlement_quality/2026-08-18-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-18), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-18-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-18-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-18"
}
```

</details>

<a id="citation-8"></a>
<details><summary>settlement_quality/2026-08-19-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-19), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-19-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-19-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-19"
}
```

</details>

<a id="citation-9"></a>
<details><summary>settlement_quality/2026-08-20-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-20), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-20-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-20-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-20"
}
```

</details>

<a id="citation-10"></a>
<details><summary>settlement_quality/2026-08-21-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-21), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-21-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-21-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-21"
}
```

</details>

<a id="citation-11"></a>
<details><summary>settlement_quality/2026-08-22-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-22), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-22-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-22-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-22"
}
```

</details>

<a id="citation-12"></a>
<details><summary>settlement_quality/2026-08-23-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-23), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-23-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-23-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-23"
}
```

</details>

<a id="citation-13"></a>
<details><summary>settlement_quality/2026-08-24-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-24), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-24-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-24-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-24"
}
```

</details>

<a id="citation-14"></a>
<details><summary>settlement_quality/2026-08-25-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-25), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-25-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-25-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-25"
}
```

</details>

<a id="citation-15"></a>
<details><summary>settlement_quality/2026-08-26-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-26), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-26-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-26-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-26"
}
```

</details>

<a id="citation-16"></a>
<details><summary>settlement_quality/2026-08-27-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-27), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-27-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-27-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-27"
}
```

</details>

<a id="citation-17"></a>
<details><summary>settlement_quality/2026-08-28-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-28), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-28-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-28-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-28"
}
```

</details>

<a id="citation-18"></a>
<details><summary>settlement_quality/2026-08-29-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-29), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-29-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-29-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-29"
}
```

</details>

<a id="citation-19"></a>
<details><summary>settlement_quality/2026-08-30-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-30), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-30-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-30-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-30"
}
```

</details>

<a id="citation-20"></a>
<details><summary>settlement_quality/2026-08-31-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-08-31), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-31-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-08-31-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-08-31"
}
```

</details>

<a id="citation-21"></a>
<details><summary>settlement_quality/2026-09-01-9b25803e6ca8.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-09-01), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-09-01-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-09-01-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-09-01"
}
```

</details>

<a id="citation-22"></a>
<details><summary>settlement_quality/settlement_quality.json</summary>

```json
{
  "dataset": "settlement-quality-register",
  "how_to_cite": "Ailin Sun. \"Polymarket settlement quality register.\" Version 9b25803e6ca8 (2026-09-01), window 2023-12-05 to 2026-08-12. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-09-01-9b25803e6ca8.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/settlement_quality/2026-09-01-9b25803e6ca8.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/settlement_quality/settlement_quality.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "settlement-quality-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket settlement quality register",
  "version": "9b25803e6ca8",
  "version_date": "2026-09-01"
}
```

</details>

<a id="citation-23"></a>
<details><summary>zero_sum_ledger/2026-08-13-858973c2494c.json</summary>

```json
{
  "dataset": "prediction-market-zero-sum-ledger",
  "how_to_cite": "Ailin Sun. \"Polymarket zero-sum flow ledger.\" Version 858973c2494c (2026-08-13), window 2025-12-11 to 2026-08-13. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/zero_sum_ledger/2026-08-13-858973c2494c.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/zero_sum_ledger/2026-08-13-858973c2494c.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/zero_sum_ledger/zero_sum_ledger.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "zero-sum-ledger-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket zero-sum flow ledger",
  "version": "858973c2494c",
  "version_date": "2026-08-13"
}
```

</details>

<a id="citation-24"></a>
<details><summary>zero_sum_ledger/zero_sum_ledger.json</summary>

```json
{
  "dataset": "prediction-market-zero-sum-ledger",
  "how_to_cite": "Ailin Sun. \"Polymarket zero-sum flow ledger.\" Version 858973c2494c (2026-08-13), window 2025-12-11 to 2026-08-13. Retrieved from https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/zero_sum_ledger/2026-08-13-858973c2494c.json",
  "immutable_url": "https://github.com/ailinsun/MarketFlow/blob/v0.1.2/data/zero_sum_ledger/2026-08-13-858973c2494c.json",
  "latest_url": "https://github.com/ailinsun/MarketFlow/blob/main/data/zero_sum_ledger/zero_sum_ledger.json",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "method_version": "zero-sum-ledger-v0.1",
  "note": "cite the immutable_url — latest_url is a rolling pointer and its numbers change when the window advances",
  "publisher": "Ailin Sun",
  "title": "Polymarket zero-sum flow ledger",
  "version": "858973c2494c",
  "version_date": "2026-08-13"
}
```

</details>

## Public attribution update

Release v0.1.2 standardizes attribution as Ailin Sun, an independent researcher, and points citation links to MarketFlow. Research numbers, dates, windows and snapshot filenames are unchanged. Schema namespace strings are synchronized across the data and instruments; schema constraints and annotations are unchanged.
