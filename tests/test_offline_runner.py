#!/usr/bin/env python3
"""The offline runner may skip a suite only for a missing optional install.

`checks/run_tests.py` reports a package module as skipped when one of the signing
layer's third-party packages is not installed. That rule must stay narrow: a
missing or misnamed first-party module is a broken import, and reporting it as
"skipped" would turn a failure into a green run.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_tests", ROOT / "checks/run_tests.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class OfflineRunnerSkipRule(unittest.TestCase):
    def _run_probe(self, source: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "mf_runner_probe.py").write_text(source, encoding="utf-8")
            before = os.environ.get("PYTHONPATH")
            os.environ["PYTHONPATH"] = tmp + (os.pathsep + before if before else "")
            try:
                return runner.run_module("mf_runner_probe")
            finally:
                if before is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = before

    def test_missing_first_party_import_fails(self):
        status, _detail = self._run_probe("import mf_first_party_module_that_does_not_exist\n")
        self.assertEqual(status, "FAIL")

    def test_missing_optional_dependency_skips(self):
        # A submodule that does not exist in any release: the probe skips whether
        # or not the optional package itself is installed.
        status, _detail = self._run_probe("import polymarket.mf_probe_missing_submodule\n")
        self.assertEqual(status, "SKIP")


if __name__ == "__main__":
    unittest.main()
