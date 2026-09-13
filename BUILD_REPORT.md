# Build and publication review

Reviewed 2026-09-13. This report supersedes the import and help failures in the initial build report. The publication result below records the release, with a subsequent attribution correction.

## Scope

The review retained standalone fee, ledger, farm-signature and UMA methods; frozen aggregates and schemas; historical working reports; and a public AI-methodology charter. The original private repository was only read for publication-kit status and its entry instructions. No commands were run there and no private files were modified.

## Results

- PASS: all Python help entry points run offline, with network attempts rejected even if caught by the instrument.
- PASS: all five retained original instrument self-tests.
- PASS: synthetic end-to-end ledger input, excluded-fill accounting and aggregate-only farm output.
- PASS: frozen ledger identities and band totals, settlement group totals, farm rank rates, exposure structure and contract-pair field checks.
- PASS: 11,647 numeric and boolean JSON values are unchanged against the supplied export.
- PASS: measurement-number tokens match in all 16 retained reports after excluding removed internal reference numbers and path targets.
- PASS at initial publication: public-file and full reachable-history privacy checks returned zero residual matches; gitleaks found no secrets in the reviewed root history. Later attribution changes do not erase earlier commits or archived copies.
- Privacy review: trader addresses and usernames, infrastructure identifiers, local paths, private references and unavailable links were reviewed and scrubbed. The allowlist is documented in data/README.md.
- Dependency review: retained code uses the Python standard library only; unused and unavailable imports were removed.
- Initial gitleaks run: two original commits scanned with no secret findings. Additional privacy checks nevertheless found infrastructure and address-like test fixtures; a secret scan alone was insufficient.

## Resolved initial failures and warnings

- Removed the import-time market daemon instead of retaining a background collector in the research release.
- Removed private snapshot generators and their helper; data/README.md explains extraction methods and missing inputs.
- Removed the structural scanner, including missing symbolic-compiler and Wolfram branches.
- Removed the retired calibration bridge and unrelated archived effective-sample estimator; no NumPy requirement remains.
- Removed the vendor trial pipeline and broken private-package test imports; retained initial annotations and their schema.
- Replaced unavailable relative links and internal note references with public links where a retained equivalent exists, or plain text.
- Rebuilt this report so old failure messages cannot disclose local paths.

## Reproduction boundaries

Aggregate consistency is verified; the historical raw datasets and complete original collection pipeline are not distributed. Passing synthetic self-tests does not certify all historical report conclusions. The contract-pair annotations retain their original initial review status. The publication kit is still marked as drafts awaiting audit, so no new English article is treated as an audited release. The original ledger truncation metadata has a textual inconsistency documented in data/README.md; it was not silently rewritten.

## History handling

The two original local commits include material removed by this review. Publish only the new reviewed root history. The original export is retained separately for local recovery; neither its branch nor its objects should be pushed or bundled into a release. The obsolete reachable publication history was replaced at the author's request. Use the current public branch and tag; do not merge older local histories. The pair schema retains its constraints with a valid public URI identifier.

## Removed or moved files

| Original file | Reason |
|---|---|
| `data/contract_equivalence/pipeline.py` | Vendor trial benchmark with private module references; retain the public labels and schema, validate them offline. |
| `data/contract_equivalence/test_pipeline.py` | Vendor trial benchmark with private module references; retain the public labels and schema, validate them offline. |
| `governance/260815_secure-trusted-engineering-constitution.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/aide-market-intel.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/cap-failsafe.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/key-wallet-security.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/lex-compliance.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/peace-chief-of-staff.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/pen-appsec.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/red-llm-redteam.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/sage-copy-audit.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/true-product-data.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/win-growth.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/agents/x-adversary.md` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/guard_money_surface.sh` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `governance/session_preflight.py` | Deployment-specific governance, prompts or hooks reveal internal topology or depend on private files; retained a public methodology charter and standalone check hook. |
| `instruments/calibration_math.py` | Retired Wolfram transport with unusable stubs and undeclared scikit-learn test imports. |
| `instruments/chain.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/kalshi_market_structure_census.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/n_eff_estimator.py` | Unrelated archived crypto decision-log analysis; its original inputs are not public. |
| `instruments/polymarket_complete_set_scanner.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_farming_carry.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_fill_model.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_flb_backtest.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_holding_exposure.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_maker_book_sampler.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_maker_markout.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_maker_sample_audit.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_maker_yield_probe.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_markets.py` | Daemon collector performs work during help and depends on operational feed layout. |
| `instruments/polymarket_microstructure_signal.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_position_sizer.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_price_ws.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_resolution_verifier.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_smart_money.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_structural_mispricing.py` | Private symbolic compiler and Wolfram branches; superseded here by the historical reports. |
| `instruments/polymarket_vault_fillsim.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_volume_census.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/polymarket_whale_profiler.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/prediction_market_cross_venue_spread.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/publish/generate_clean_data.py` | Private snapshot-generation pipeline and unavailable inputs; public extraction and replay limits documented in data/README.md. |
| `instruments/publish/generate_settlement_quality.py` | Private snapshot-generation pipeline and unavailable inputs; public extraction and replay limits documented in data/README.md. |
| `instruments/publish/generate_zero_sum_ledger.py` | Private snapshot-generation pipeline and unavailable inputs; public extraction and replay limits documented in data/README.md. |
| `instruments/publish/publish_common.py` | Private snapshot-generation pipeline and unavailable inputs; public extraction and replay limits documented in data/README.md. |
| `instruments/rotation.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/settlement/settlement_intelligence.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/settlement/uma_onchain.py` | Moved unchanged research reader to the flat instruments directory, then scrubbed test fixtures. |
| `instruments/uma_dispute_concentration.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `instruments/whale_report_dataset.py` | Operational collector or adjacent strategy instrument without a frozen public input; not needed by the retained offline methods. |
| `reports/zh/260618_prediction-market-money-edge-evidence-verdict.md` | External literature and trading-product verdict; peripheral to the frozen first-hand data. |
| `reports/zh/260705_competitor-landscape-and-edge-thesis.md` | Competitor handles, identifiable trader examples, and internal positioning. |
| `reports/zh/260726_retail-behavior-profile.md` | Detailed behavioural profiling and contact-oriented research outside the aggregate publication scope. |
| `reports/zh/260728_vol-organ-sizing-verdict.md` | Internal strategy and sizing decisions without public input artifacts. |
| `reports/zh/260805_trade-arrival-hawkes-first-fit.md` | Operational pipeline latency and retired model history without public fit inputs. |
| `reports/zh/260813_industry-vision-and-legitimacy.md` | Internal positioning, outreach and regulatory mapping; not an audited public article. |

## Publication result

The reviewed repository is public under ailinsun/MarketFlow. Release v0.1.1 was archived at [DOI 10.5281/zenodo.22733782](https://doi.org/10.5281/zenodo.22733782). That obsolete archive was subsequently withdrawn. All three Hugging Face data mirrors use CC BY 4.0 dataset cards and licences. Public downloads of the prepared files matched the reviewed local files byte for byte. The README links the data mirrors. The detailed execution record remains in the uncommitted PUBLISH_REPORT.md.

## Attribution correction

Public author: Ailin Sun, independent researcher. Repository: MarketFlow. Current files, embedded citations, schema namespaces and instrument identifiers use the corrected attribution. All 11,647 numerical and boolean data values and measurement-number tokens in the retained reports remain unchanged. The methodology charter now describes experimental research review. Reachable repository history has been rebuilt from the corrected files, and the obsolete archive has been withdrawn. Platform caches and external archives require separate verification; clean reachable history alone does not establish their removal.

Corrected release: [v0.1.2](https://github.com/ailinsun/MarketFlow/releases/tag/v0.1.2), [DOI 10.5281/zenodo.22734802](https://doi.org/10.5281/zenodo.22734802). The prior record metadata was also corrected to Sun, Ailin with no affiliation.
