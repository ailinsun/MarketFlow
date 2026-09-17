# 004 — Correlation sources stay separate

## Decision

Correlation enters portfolio risk from three places, and they are carried as three
separate quantities with different epistemic status. They are never merged into one
number.

| Source | Status | Default |
|---|---|---|
| Within a mutually exclusive group | computed from structure and implied prices | not a parameter |
| Non-exclusive legs on one event | assumed | 1.0, the conservative end |
| Across events, same theme | measured, and used only if a pre-registered test confirms it | 0.0 |

## Why

Merging them produces a single number in which an exact structural identity, a
conservative guess and a weak empirical estimate are indistinguishable. A later reader
cannot tell which parts of a risk figure are derived and which were invented, and the
invented parts are the ones that need revisiting when the model is wrong.

## The pre-registration

The thematic layer is the only one that could be fitted, so it carries the heaviest
procedure: criteria fixed before the result is read, a date-stratified permutation null
that removes the same-day confound, a cluster-level bootstrap, and a **positive
control** — a relationship whose sign is known from structure must reproduce before any
measured number is read at all. A failed control makes every verdict in that run
`INSTRUMENT_UNVALIDATED` rather than merely negative.

Residuals are mean-centred first. A book with a systematic edge has a positive mean
residual, and the expected product of any two residuals then carries that mean squared,
making every cluster look positively correlated. That measures the edge, not
correlation.

## The standing result

Cross-event thematic correlation is statistically detectable in a whole-market sample
and too small to use, and undetectable in a single book. The engine therefore uses
zero. A criterion that says zero means zero: the value of the procedure is that it can
return "no" rather than a small number that looks like information.

## What would make this wrong

A regime in which thematic correlation matters — a market structure where many events
share a common driver strongly enough to move the estimate above the threshold. The
test is re-runnable; that is the point of pre-registering it rather than hard-coding
the conclusion.
