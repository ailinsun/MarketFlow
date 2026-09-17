# Corrections and contributions

Active development has ended, so treat this as an archive that still runs. Corrections
are welcome; new features are unlikely to be merged.

Use [Issues](https://github.com/ailinsun/MarketFlow/issues) for reproducible bugs,
evidence-backed corrections or method questions. Include the file, release or commit,
the observation window, the expected result and the evidence you have.

## Before opening a pull request

```sh
make verify     # the publication gate plus every self-test, offline
make history    # the gate over the commits this branch adds
make demo       # the end-to-end path still produces the documented numbers
```

`make verify` needs nothing installed. Say which checks you ran and keep changes small.

## What the checks will reject

The publication gate fails on identifiers, credentials, wallet addresses outside the
reviewed allowlist, private paths, internal references, unfilled placeholders,
undeclared imports and broken relative links. It also fails on non-Latin script outside
`reports/` and `data/`, which hold dated documents in the language they were written
in.

The test runner denies network access through an audit hook that records an attempt
even when the code under test catches the exception. If a change makes a self-test
reach the network, the suite fails — add an injectable seam and a stub rather than an
exemption.

Binary files are pinned by digest. A changed or added binary fails until it is reviewed
and its digest updated in `checks/check_public.py`.

## Norms

- **Use synthetic or aggregate examples.** Never post wallet lists, participant
  identities, credentials, private paths or contact details.
- **Keep historical measurements intact.** Propose corrections with their source and an
  explanation; do not silently replace a number in a dated report.
- **A new limit is a fraction, not a dollar amount.** See
  [design decision 002](../docs/design-decisions/002-limits-are-fractions-of-a-capital-base.md).
- **A new default is the one that refuses.** See
  [design decision 003](../docs/design-decisions/003-unset-means-closed.md).
- **A new claim in the documentation needs something inspectable behind it** — code, a
  test, a fixture or a frozen result with provenance.
- Contract-pair labels remain provisional until independently reviewed.

## Security

Report anything that would expose keys or funds through an issue that says only that
you have a private report and asks for a contact channel. See
[SECURITY.md](../SECURITY.md).
