#!/usr/bin/env python3
"""Effective sample size of an autocorrelated series.

A run of 5,000 observations from a correlated process does not carry 5,000
observations' worth of evidence. Treating it as if it does is the single most
common way a backtest reports a p-value it has not earned: the null distribution
is built as though each point were independent, so anything persistent looks
significant. This module measures how much independent evidence a series actually
contains, so a claim can be sized to it.

The quantity is the integrated autocorrelation time:

    tau_int(W) = 1/2 + sum_{k=1..W} rho(k)
    n_eff      = N / (2 * tau_int)

For white noise tau_int is 1/2 and n_eff is N. For a positively autocorrelated
series tau_int grows and n_eff falls. An anti-correlated series can push tau_int
below 1/2 — n_eff is then clamped at N, because no estimator gets more evidence
than it has observations.

Everything turns on where the sum is truncated. Summing every lag accumulates
noise without bound; truncating too early understates the correlation. Two
published windowing rules are implemented, and they are reported side by side
rather than averaged, because disagreement between them is itself information:

  * **Wolff (2003)**, arXiv hep-lat/0306017 section 3.3, equations 50-52 — the
    primary estimator. It chooses the smallest window W where the estimated bias
    stops dominating the estimated noise:

        tau(W) = S / ln((2*tau_int(W) + 1) / (2*tau_int(W) - 1))      (eq 51)
        g(W)   = exp(-W / tau(W)) - tau(W) / sqrt(W * N)              (eq 52)
        W_opt  = the smallest W with g(W) < 0

    S = 1.5 follows the paper's "S = 1...2 is reasonable" and the UWerr reference
    implementation's default.

  * **Sokal-style self-consistent windowing** — the smallest M with
    M >= c * tau_int(M), conventionally c = 5. Kept as a cross-check only. The
    constant is a textbook convention rather than a result verified against a
    primary source, and the code says so where it is used.

Both return a flag when the window never closed inside the available lags, which
means the series is too short relative to its own correlation time for either rule
to be trusted.

Standard library only; no data dependency. Use it on returns, residuals, or any
series whose independence a later test is about to assume.

    python3 -B instruments/n_eff_estimator.py --selftest
    python3 -B instruments/n_eff_estimator.py --jsonl obs.jsonl --field residual
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Iterable, Sequence

WOLFF_S = 1.5          # Wolff 2003 p.13; UWerr.m default
SOKAL_C = 5.0          # textbook convention, not first-hand verified
MAX_LAG_FRACTION = 0.25
MIN_OBSERVATIONS = 32


def autocorrelation(series: Sequence[float], n_lags: int) -> list[float]:
    """Biased, normalised autocorrelation rho(0..n_lags).

    Biased (dividing every lag by N rather than by N-k) is the right choice here:
    it is what the integrated-autocorrelation-time literature assumes, and the
    unbiased form's variance explodes at long lags, which is exactly where the
    windowing rules have to make their decision.
    """
    n = len(series)
    if n == 0:
        return [1.0]
    mean = sum(series) / n
    dev = [x - mean for x in series]
    c0 = sum(d * d for d in dev) / n
    if c0 <= 0.0:                       # a constant series has no correlation structure
        return [1.0] + [0.0] * n_lags
    out = [1.0]
    for k in range(1, min(n_lags, n - 1) + 1):
        ck = sum(dev[i] * dev[i + k] for i in range(n - k)) / n
        out.append(ck / c0)
    return out


def wolff_tau_int(rho: Sequence[float], n_obs: int, s: float = WOLFF_S) -> tuple[float, int, bool]:
    """Wolff 2003 automated windowing. Returns (tau_int, W_opt, window_never_closed)."""
    n_lags = len(rho) - 1
    cum = 0.5
    for w in range(1, n_lags + 1):
        cum += rho[w]
        if cum <= 0.5:
            tau_w = 1e-6                # anti-correlated: the window closes immediately
        else:
            arg = (2.0 * cum + 1.0) / (2.0 * cum - 1.0)
            denom = math.log(arg)
            tau_w = s / denom if denom > 0 else 1e-6
        g = math.exp(-w / tau_w) - tau_w / math.sqrt(w * n_obs)
        if g < 0:
            return cum, w, False
    return cum, n_lags, True


def sokal_tau_int(rho: Sequence[float], c: float = SOKAL_C) -> tuple[float, int, bool]:
    """Self-consistent windowing: the smallest M with M >= c * tau_int(M).

    Cross-check only. c = 5 is a common default; it is not derived here and is not
    verified against a primary source, so a disagreement with the Wolff estimate
    should be read as "look at the series", not as "the Wolff number is wrong".
    """
    n_lags = len(rho) - 1
    cum = 0.5
    for m in range(1, n_lags + 1):
        cum += rho[m]
        if m >= c * cum:
            return cum, m, False
    return cum, n_lags, True


def effective_sample_size(series: Sequence[float], *, max_lag_fraction: float = MAX_LAG_FRACTION,
                          s: float = WOLFF_S, c: float = SOKAL_C) -> dict:
    """Full report for one series: both windowing rules, tau_int, n_eff, diagnostics."""
    n = len(series)
    if n < MIN_OBSERVATIONS:
        return {"status": "insufficient_data", "n_obs": n,
                "min_observations": MIN_OBSERVATIONS}
    n_lags = min(max(int(max_lag_fraction * n), 8), n - 1)
    rho = autocorrelation(series, n_lags)
    tau_w, w_opt, open_w = wolff_tau_int(rho, n_obs=n, s=s)
    tau_s, m_opt, open_s = sokal_tau_int(rho, c=c)
    # tau_int < 1/2 means anti-correlation. n_eff is capped at n: an estimator never
    # holds more independent evidence than it has observations.
    tau_used = max(tau_w, 0.5)
    return {
        "status": "ok",
        "n_obs": n,
        "n_lags_examined": n_lags,
        "tau_int_wolff": tau_w,
        "window_wolff": w_opt,
        "window_never_closed_wolff": open_w,
        "tau_int_sokal_crosscheck": tau_s,
        "window_sokal": m_opt,
        "window_never_closed_sokal": open_s,
        "anti_correlated": tau_w < 0.5,
        "n_eff": n / (2.0 * tau_used),
        "independence_ratio": (n / (2.0 * tau_used)) / n,
        "rho_lag1": rho[1] if n_lags >= 1 else None,
        "rho_lag5": rho[5] if n_lags >= 5 else None,
        "rho_lag20": rho[20] if n_lags >= 20 else None,
    }


def _ar1(n: int, phi: float, seed: int = 7) -> list[float]:
    """An AR(1) series with a known integrated autocorrelation time.

    rho(k) = phi**k exactly in the population, so
        tau_int = 1/2 + sum_{k>=1} phi**k = 1/2 + phi / (1 - phi).
    """
    import random
    rng = random.Random(seed)
    scale = math.sqrt(1.0 - phi * phi)
    x = rng.gauss(0.0, 1.0)
    out = []
    for _ in range(n):
        x = phi * x + scale * rng.gauss(0.0, 1.0)
        out.append(x)
    return out


def ar1_tau_int(phi: float) -> float:
    """Population integrated autocorrelation time of an AR(1) process."""
    return 0.5 + phi / (1.0 - phi)


def selftest() -> int:
    checks: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # White noise: rho(k) ~ 0, tau_int ~ 1/2, n_eff ~ n.
    white = _ar1(4000, 0.0, seed=1)
    r = effective_sample_size(white)
    ck("white noise: tau_int is about one half", abs(r["tau_int_wolff"] - 0.5) < 0.15,
       f"{r['tau_int_wolff']:.4f}")
    ck("white noise: keeps most of its sample", r["independence_ratio"] > 0.75,
       f"{r['independence_ratio']:.3f}")

    # AR(1) with phi = 0.8: tau_int = 0.5 + 0.8/0.2 = 4.5, n_eff = n/9.
    phi = 0.8
    r = effective_sample_size(_ar1(20000, phi, seed=2))
    want = ar1_tau_int(phi)
    ck("AR(1): tau_int matches the closed form within 20%",
       abs(r["tau_int_wolff"] - want) / want < 0.20, f"{r['tau_int_wolff']:.3f} vs {want:.3f}")
    ck("AR(1): n_eff is far below n", r["independence_ratio"] < 0.2,
       f"{r['independence_ratio']:.3f}")
    ck("AR(1): lag-1 autocorrelation recovers phi", abs(r["rho_lag1"] - phi) < 0.05,
       f"{r['rho_lag1']:.3f}")

    # Monotonicity: more correlation must never mean more independent evidence.
    ratios = [effective_sample_size(_ar1(8000, p, seed=3))["independence_ratio"]
              for p in (0.0, 0.3, 0.6, 0.9)]
    ck("stronger correlation never raises the independence ratio",
       all(a >= b - 1e-9 for a, b in zip(ratios, ratios[1:])),
       ", ".join(f"{x:.3f}" for x in ratios))

    # The two windowing rules must broadly agree on a well-behaved series.
    r = effective_sample_size(_ar1(20000, 0.7, seed=4))
    ck("Wolff and Sokal windows agree within a factor of two",
       0.5 <= r["tau_int_wolff"] / r["tau_int_sokal_crosscheck"] <= 2.0,
       f"{r['tau_int_wolff']:.3f} vs {r['tau_int_sokal_crosscheck']:.3f}")

    # Anti-correlation is reported, and n_eff is still capped at n.
    alt = [(-1.0) ** i for i in range(2000)]
    r = effective_sample_size(alt)
    ck("an alternating series is flagged anti-correlated", r["anti_correlated"] is True,
       f"{r['tau_int_wolff']:.4f}")
    ck("n_eff never exceeds n", r["n_eff"] <= r["n_obs"] + 1e-9,
       f"{r['n_eff']:.1f} vs {r['n_obs']}")

    # Degenerate inputs fail loudly rather than returning a confident number.
    ck("a constant series has no correlation structure",
       effective_sample_size([3.0] * 500)["tau_int_wolff"] == 0.5)
    ck("too few observations is refused, not guessed",
       effective_sample_size([1.0, 2.0, 3.0])["status"] == "insufficient_data")

    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    failed = [c for c in checks if not c[1]]
    print(f"\nselftest: {len(checks) - len(failed)}/{len(checks)} "
          + ("ALL PASS" if not failed else "FAILURES"))
    return 1 if failed else 0


def _read_field(path: str, field: str) -> list[float]:
    out: list[float] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            v = row.get(field)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                out.append(v)
    return out


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="run the offline self-test")
    ap.add_argument("--jsonl", help="JSONL file, one observation per line")
    ap.add_argument("--field", help="numeric field to read from each JSONL row")
    ap.add_argument("--max-lag-fraction", type=float, default=MAX_LAG_FRACTION,
                    help="longest lag examined, as a fraction of the series length")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return selftest()
    if not (args.jsonl and args.field):
        ap.print_help()
        return 0
    series = _read_field(args.jsonl, args.field)
    print(json.dumps(effective_sample_size(series, max_lag_fraction=args.max_lag_fraction),
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
