# 003 — Unset means closed

## Decision

Every gate defaults to the state that refuses. An unconfigured system does nothing
rather than everything, and every fallback is chosen so failure tightens rather than
loosens.

| Condition | Result |
|---|---|
| `MARKETFLOW_ENTRY_SOURCES` unset | no signal source may open a position |
| entitlement module unset | nothing is entitled |
| arm state unreadable | dry run |
| fee rate unavailable | a *higher* assumed fee |
| filter file missing | no filtering, with the source recorded in the output |
| alert sink unconfigured | the alert is dropped and the call returns `False` |

## Why

The alternative is a system whose safety depends on someone having remembered to
configure it. Defaults are load-bearing precisely because nobody reads them.

The fee case shows the shape: an unknown fee assumed to be zero makes every trade look
more profitable than it is, and a venue outage would then loosen the gate. A higher
assumed fee can only reject trades that would otherwise have been marginal.

The alert case shows the limit of the rule: an alert sink that raised on failure would
take down the thing it is watching. So it fails soft — but it reports `False` rather
than silently claiming success, because "the alert was sent" and "the alert was
attempted" are different facts.

## What this rules out

- **Convenience defaults that widen a limit.** There are none. A default may only be
  set to a value that is safe when nobody has thought about it.
- **Silently permissive degradation.** Where a component degrades, the degradation is
  recorded in the output, so a reader can tell a filtered result from an unfiltered one.

## Enforcement

The unset-means-closed paths are covered by module self-tests: a live request without
arm state resolving to dry run, an unconfigured entitlement module denying entry, an
empty entry-source allow-list admitting nothing.

## What would make this wrong

Nothing about the principle. The cost is real, though: a misconfigured deployment looks
broken rather than dangerous, and an operator who does not read the logs may conclude
the system does not work. That is the intended trade.
