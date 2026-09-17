#!/usr/bin/env python3
"""The runtime state tree must match the package layout, and watchers must watch
paths that writers write.

This exists because they diverged once and nothing noticed: the money-path watchdog
was moved to the published layout while the writer kept the old one, so the watchdog
of the money path watched a file that was never created. Its own self-test passed,
because it only checked that a path resolved, not that anything wrote there. A
heartbeat monitor that silently watches the wrong path is worse than no monitor: it
reports healthy forever and it is the last line of defence.
"""
from __future__ import annotations

import os
import pathlib
import re
import unittest

from marketflow import runtime_dir
from marketflow.monitor import watchdog
from marketflow.risk import money_path

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_AREAS = {"feeds", "risk", "execution", "guardian", "monitor", "mcp"}
# Areas that may appear as the first segment of a runtime path but are not packages.
NON_PACKAGE_AREAS = {"archive", "logs", "instruments"}
# Segments from the private repository's layout. None may survive.
RETIRED_SEGMENTS = ("trade", "analysis", "alerts", "hq", "web")


class TestWatchersWatchWhatWritersWrite(unittest.TestCase):
    def test_money_path_heartbeat_agrees(self):
        target = next(t for t in watchdog.TARGETS if t[0] == "money-path-watchdog")
        self.assertEqual(
            money_path.LATEST_PATH, watchdog._resolve(target[1]),
            "the money-path watchdog is watching a path nothing writes")

    def test_every_execution_side_target_resolves_under_the_runtime_tree(self):
        for label, rel, _max_age, _human in watchdog.TARGETS:
            resolved = watchdog._resolve(rel)
            if rel.startswith(watchdog.EXEC_PREFIX):
                self.assertTrue(resolved.startswith(runtime_dir()),
                                f"{label} resolves outside the runtime tree: {resolved}")

    def test_the_two_buckets_never_collide(self):
        """A mirrored heartbeat written to the service's own bucket is never read."""
        own = watchdog._resolve("state.json")
        mirrored = watchdog._resolve("execution/anything.json")
        self.assertNotEqual(os.path.dirname(own), os.path.dirname(mirrored))


class TestRuntimeLayoutMatchesPackageLayout(unittest.TestCase):
    def test_no_module_writes_into_the_source_tree(self):
        """Generated output belongs in the runtime tree, never beside committed files."""
        offenders = []
        for p in sorted((ROOT / "marketflow").rglob("*.py")):
            for m in re.finditer(r'os\.path\.join\(\s*(?:REPO|REPO_ROOT|PROJECT_DIR)\s*,\s*"([a-z]+)"',
                                 p.read_text()):
                if m.group(1) in ("docs", "data", "reports", "instruments", "examples", "tests"):
                    offenders.append(f"{p.relative_to(ROOT)}: writes under {m.group(1)}/")
        self.assertEqual(offenders, [])

    def test_no_retired_layout_segment_survives(self):
        offenders = []
        for p in sorted((ROOT / "marketflow").rglob("*.py")):
            for m in re.finditer(r'runtime_path\(\s*"([a-z_]+)"', p.read_text()):
                if m.group(1) in RETIRED_SEGMENTS:
                    offenders.append(f"{p.relative_to(ROOT)}: runtime_path(\"{m.group(1)}\"")
        self.assertEqual(offenders, [])

    def test_every_runtime_area_is_a_package_or_a_declared_exception(self):
        seen = set()
        for p in sorted((ROOT / "marketflow").rglob("*.py")):
            seen |= set(re.findall(r'runtime_path\(\s*"([a-z_]+)"', p.read_text()))
        unknown = seen - PACKAGE_AREAS - NON_PACKAGE_AREAS
        self.assertEqual(unknown, set(), f"undeclared runtime areas: {sorted(unknown)}")

    def test_a_module_writes_under_its_own_area(self):
        """marketflow/risk/x.py may not put its output under runtime/execution/."""
        offenders = []
        for p in sorted((ROOT / "marketflow").rglob("*.py")):
            parts = p.relative_to(ROOT / "marketflow").parts
            if len(parts) < 2 or parts[0] not in PACKAGE_AREAS:
                continue
            src = p.read_text()
            for m in re.finditer(r'^(?:OUT_DIR|_OUT_DIR)\s*=\s*runtime_path\(\s*"([a-z_]+)"',
                                 src, re.M):
                if m.group(1) != parts[0]:
                    offenders.append(f"{p.relative_to(ROOT)}: OUT_DIR under {m.group(1)}/")
        self.assertEqual(offenders, [])

    def test_the_runtime_root_is_relocatable(self):
        """An installed package must not write beside its own source."""
        self.assertTrue(runtime_dir().startswith(str(ROOT)))
        prev = os.environ.get("MARKETFLOW_RUNTIME")
        os.environ["MARKETFLOW_RUNTIME"] = "/tmp/marketflow-relocated"
        try:
            self.assertEqual(runtime_dir(), "/tmp/marketflow-relocated")
        finally:
            if prev is None:
                os.environ.pop("MARKETFLOW_RUNTIME", None)
            else:
                os.environ["MARKETFLOW_RUNTIME"] = prev


if __name__ == "__main__":
    unittest.main(verbosity=2)
