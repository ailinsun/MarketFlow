# Publication checks

Two programs, both run by `make verify`.

## check_public.py — the publication gate

Scans every file for identifiers, credentials, private paths and references, wallet
addresses outside a reviewed allowlist, unfilled placeholders, undeclared imports,
broken relative links, and non-Latin script outside the directories that hold dated
documents in their original language.

Binary files are never scanned as text. Each is pinned to the SHA-256 it was reviewed
at, so a changed or added binary fails until a human looks at it and updates the digest.

```sh
make check      # the working tree
make history    # plus the commits this release adds
```

### Two history modes, and why

`--history` scans the commits added since `PUBLISHED_BASELINE` and **skips blobs
already reachable from it**. Content byte-identical to something already public cannot
be a new disclosure, and without that rule every intermediate commit on a release
branch re-reports the entire published history and buries the signal.

`--history-all` scans everything, including the already-published record. It reports
several hundred matches, and **that is expected**: the v0.1.2 tree carried Chinese
docstrings in the research instruments and a personal name in its README, its citation
metadata and its commit metadata. That history is public, archived under a DOI and
cited; rewriting it would break the citation chain that points into it and would
protect nothing, because the content is already distributed.

The policy therefore applies to the **current tree**, and it has no exemptions: no file
here may name a person, including `CITATION.cff`, `.zenodo.json` and the `how_to_cite`
and `publisher` fields embedded in the frozen snapshots. Attribution is the project and
the GitHub account; provenance for earlier releases travels through DOIs, version
identifiers and immutable URLs. The release is judged on `--history`, which is clean.

### Negative controls

A scanner that has quietly stopped matching looks exactly like a clean repository.
[`tests/test_publication_gate.py`](../tests/test_publication_gate.py) asserts that each
rule fires on its own violation, and that the positive controls do not fire — synthetic
addresses, reserved-TLD emails, decorators, the citation-metadata exemption and dated
reports keeping their language. Its fixtures are assembled at runtime so that file does
not itself contain the strings it tests for, which keeps the gate free of exemption
lists.

Pattern checks supplement manual review. They do not establish that every possible form
of identifying text has been detected, which is why `gitleaks` runs as an independent
second layer in CI and should be run before any push.

## run_tests.py — the offline test runner

Runs every help entry point, every instrument self-test, the frozen-aggregate
verification, the synthetic end-to-end input flows, every `marketflow` module
self-test, the signing-layer suites, the unit tests and the demonstration.

It installs an audit hook that denies network access and **records an attempt even when
the code under test catches the exception**. Several reads in this system are
deliberately fail-soft; without that, a self-test could reach the venue, swallow the
result and report success. Two such reads were found this way.

A module whose optional third-party dependency is absent is reported as `SKIP` with the
install command, never as a pass.
