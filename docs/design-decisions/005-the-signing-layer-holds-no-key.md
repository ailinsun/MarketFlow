# 005 — The signing layer holds no key

## Decision

The service never holds a private key. Keys live in a signing enclave, and the service
requests signatures under a scoped policy that **cannot express a transfer out**. Root
authority over that configuration requires a quorum of at least two signers.

## Why

The property being bought is not "the code does not withdraw funds". Code can be wrong,
and a reviewer cannot verify the absence of a behaviour by reading it. The property is
that the authority the service holds is insufficient to withdraw — so a total
compromise of the service yields the ability to place orders within a mandate, and
nothing else.

This is a boundary a reviewer can check in one place, against a policy document, rather
than by auditing every path that touches a wallet.

## What this rules out

- **Any local key material**, including "temporarily" during onboarding. The legacy
  hosted path that predates this is disabled behind
  `MARKETFLOW_ALLOW_LEGACY_HOSTED_ONBOARDING` and defaults off.
- **A single-signature root.** `MARKETFLOW_MIN_ROOT_QUORUM` is floored at 2 in code: a
  lower value is ignored, a higher one honoured. `guardian.authority` refuses a
  single-signature root configuration and a self-test asserts the refusal, so the
  property cannot be lost to a configuration change.

## Cost

A signing enclave is an external dependency with its own availability and trust
assumptions, and the venue SDK is pinned to a specific commit rather than a release —
a rebuild must not silently change code that produces signatures. Verifying that commit
is a manual step, and [SECURITY.md](../../SECURITY.md) says so.

## What would make this wrong

An enclave policy language expressive enough to be talked into a withdrawal, or a venue
whose order-placement authority is not separable from transfer authority. The second
would make the whole design unavailable rather than merely weaker, and the honest
response would be to say so rather than to approximate it in code.
