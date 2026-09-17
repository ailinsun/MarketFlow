#!/usr/bin/env python3
"""The central claim of the risk design: limits are ratios, not dollar constants.

If any cap in this system were a hard-coded dollar amount, it would be a statement
about one particular book — and the first thing a different deployment would have to
do is edit the source. These tests change only the declared capital base and assert
that every limit moves with it exactly, and that the ceilings stay ceilings.

The constants are resolved at import time, so each base is measured in its own
subprocess rather than by reloading a module and hoping nothing cached.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

PROBE = """
import json
from marketflow.execution import orders
out = {
    "base": orders.REFERENCE_CAPITAL_USD,
    "total": orders.DEFAULT_MAX_TOTAL_DEPLOY_USD,
    "per_trade": orders.DEFAULT_MAX_PER_TRADE_USD,
    "drawdown": orders.DEFAULT_MAX_DRAWDOWN_USD,
    "ceiling_total": orders.OWNER_CAP_CEILING_TOTAL_USD,
    "ceiling_per_trade": orders.OWNER_CAP_CEILING_PER_TRADE_USD,
    "ceiling_drawdown": orders.OWNER_CAP_CEILING_DRAWDOWN_USD,
}
try:                       # the mandate tiers need the optional signing install
    from marketflow.guardian import executor
    out.update({
        "mandate_base": executor.MANDATE_CAPITAL_USD,
        "tenant_ceiling_total": executor.TENANT_CAP_CEILING_TOTAL_USD,
        "prime_ceiling_total": executor.PRIME_CAP_CEILING_TOTAL_USD,
        "prime_ceiling_per_trade": executor.PRIME_CAP_CEILING_PER_TRADE_USD,
    })
except ImportError:
    pass
print(json.dumps(out))
"""

BASES = (100_000.0, 1_000_000.0, 50_000_000.0, 500_000_000.0)


def limits_at(base: float) -> dict:
    env = dict(os.environ,
               MARKETFLOW_CAPITAL_BASE_USD=repr(base),
               MARKETFLOW_MANDATE_CAPITAL_USD=repr(base))
    out = subprocess.run([sys.executable, "-B", "-c", PROBE], env=env, cwd=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(out.stderr[-600:])
    return json.loads(out.stdout)


class TestScaleInvariance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.measured = {b: limits_at(b) for b in BASES}

    @property
    def has_mandate_tiers(self) -> bool:
        return "mandate_base" in self.measured[BASES[0]]

    def test_the_declared_base_is_the_base_used(self):
        for base, m in self.measured.items():
            self.assertEqual(m["base"], base)
            if self.has_mandate_tiers:
                self.assertEqual(m["mandate_base"], base)

    def test_every_limit_is_a_fixed_fraction_of_the_base(self):
        """A five-thousand-fold change in capital must not move a single ratio."""
        keys = ["total", "per_trade", "drawdown", "ceiling_total", "ceiling_per_trade",
                "ceiling_drawdown"]
        if self.has_mandate_tiers:
            keys += ["tenant_ceiling_total", "prime_ceiling_total", "prime_ceiling_per_trade"]
        ratios = {k: {b: self.measured[b][k] / b for b in BASES} for k in keys}
        for k, by_base in ratios.items():
            values = list(by_base.values())
            self.assertTrue(all(abs(v - values[0]) < 1e-12 for v in values),
                            f"{k} is not a fixed fraction: {by_base}")

    def test_no_limit_is_a_dollar_constant(self):
        """The failure this guards against: a cap that ignores the base entirely."""
        small, large = self.measured[BASES[0]], self.measured[BASES[-1]]
        for k in ("total", "per_trade", "drawdown", "ceiling_per_trade"):
            self.assertNotAlmostEqual(
                small[k], large[k],
                msg=f"{k} did not move between a $100k and a $500M base")

    def test_defaults_stay_under_their_ceilings(self):
        for base, m in self.measured.items():
            self.assertLessEqual(m["total"], m["ceiling_total"])
            self.assertLessEqual(m["per_trade"], m["ceiling_per_trade"])
            self.assertLessEqual(m["drawdown"], m["ceiling_drawdown"])

    def test_the_professional_tier_is_wider_but_still_bounded(self):
        if not self.has_mandate_tiers:
            self.skipTest("mandate tiers need the optional signing install")
        for base, m in self.measured.items():
            self.assertGreater(m["prime_ceiling_total"], m["tenant_ceiling_total"])
            self.assertLessEqual(m["prime_ceiling_total"], base)

    def test_ordering_is_preserved_at_every_scale(self):
        """Per-trade under drawdown under total, at $100k and at $500M alike."""
        for base, m in self.measured.items():
            self.assertLess(m["per_trade"], m["drawdown"], f"at base {base}")
            self.assertLess(m["drawdown"], m["total"], f"at base {base}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
