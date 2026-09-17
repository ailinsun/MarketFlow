# Cross-venue contract-pair annotations

[gold_pairs.jsonl](gold_pairs.jsonl) contains 20 Polymarket–Kalshi contract pairs labelled on 2026-08-24 from public venue rules. [gold_pair.schema.json](gold_pair.schema.json) describes each pair, relation, direction, rationale, counterexample and source evidence.

The historical filename contains “gold”, but every record is still marked `review_status: initial` and `labeler: codex:root` (an AI role, not a trader username). These are initial AI-assisted research annotations, not an independently adjudicated benchmark. The public review checks structure and privacy without upgrading label status or confidence values.

Relation labels distinguish exact equivalence, directional implication, overlap, partial hedge, mutual exclusion, exhaustive partition, non-equivalence and unknown. Similar question wording does not establish equal payout conditions. Source links capture provenance; rules and live endpoints may change.

The vendor-trial collection and comparison scripts are omitted because their private imports and external trial inputs are not distributed. `python3 instruments/verify_snapshots.py` checks the retained pair structure from the repository root.

Licence: CC BY 4.0 for the original annotations. No embedded citation block exists; cite the authors named in CITATION.cff, this repository release and the filename. [Inventory and citation policy](../README.md).
