# Publication checks

- [check_public.py](check_public.py) scans identifiers, private references, unresolved placeholders, imports and relative links. Add `--history` after creating the public commit to scan every reachable historical blob and commit metadata.
- [run_tests.py](run_tests.py) denies network attempts while testing every Python help entry, instrument self-tests, snapshot arithmetic and synthetic input flows.

Run `make check` and `make test` from the root. If installed, also run `gitleaks git --redact --no-banner .` before pushing. Pattern checks supplement manual review; they do not establish that every possible form of identifying text has been detected.
