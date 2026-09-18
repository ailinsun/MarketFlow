#!/usr/bin/env python3
"""No first-party reference may point at a name that no longer exists.

A rename leaves the *import* intact and only breaks the attribute, so an
undeclared-import check does not see it: `executor.PRIME_CAP_CEILING_TOTAL_USD`
still imports fine after `PRIME_` becomes `PROFESSIONAL_`, and it only fails on the
one line that reads it — which in a signing-only code path is exactly the line the
default test run never reaches. This test closes that gap by resolving every
attribute access on a first-party module against the module's own top-level names.

Narrow on purpose:

  * only aliases that resolve to a `marketflow.*` module or package are followed;
  * dunders and attributes named in a `hasattr(...)` probe are skipped, because a
    guarded lookup is the code's way of saying the name may be absent;
  * everything is read as an AST, never imported, so the check works without the
    optional signing install and cannot be affected by import side effects.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("marketflow", "instruments", "checks", "examples", "tests")


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def build_symbol_table(root: pathlib.Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(module -> top-level names, package -> names + submodule stems)."""
    modules: dict[str, set[str]] = {}
    packages: dict[str, set[str]] = {}
    for path in (root / "marketflow").rglob("*.py"):
        rel = path.relative_to(root).with_suffix("")
        if path.name == "__init__.py":
            mod = ".".join(rel.parts[:-1]) or "marketflow"
            package = mod
        else:
            mod = ".".join(rel.parts)
            package = mod.rsplit(".", 1)[0]
        modules[mod] = _top_level_names(ast.parse(path.read_text(encoding="utf-8")))
        bucket = packages.setdefault(package, set())
        bucket |= modules[mod]
        if path.name != "__init__.py":
            bucket.add(path.stem)
    return modules, packages


def find_dangling_references(source: str,
                             modules: dict[str, set[str]],
                             packages: dict[str, set[str]]) -> list[str]:
    """Attribute accesses on a first-party module that the module does not define."""
    tree = ast.parse(source)
    guarded = {
        node.args[1].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "hasattr" and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
    }
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("marketflow"):
            for a in node.names:
                if a.name != "*":
                    aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("marketflow"):
                    aliases[a.asname or a.name] = a.name
    problems: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        target = aliases.get(node.value.id)
        if target is None or node.attr in guarded or node.attr.startswith("__"):
            continue
        owner = target if target in modules or target in packages else target.rsplit(".", 1)[0]
        known = set(modules.get(owner, set())) | set(packages.get(owner, set()))
        if not known:
            continue
        if node.attr not in known:
            problems.append(f"{node.lineno}:{node.value.id}.{node.attr} -> {owner} has no {node.attr}")
    return problems


class TestFirstPartyReferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules, cls.packages = build_symbol_table(ROOT)

    def test_every_attribute_on_a_first_party_module_exists(self):
        problems: list[str] = []
        for directory in SCANNED_DIRS:
            base = ROOT / directory
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if any(part in {"__pycache__", "build"} for part in path.parts):
                    continue
                found = find_dangling_references(path.read_text(encoding="utf-8"),
                                                 self.modules, self.packages)
                problems.extend(f"{path.relative_to(ROOT)}:{p}" for p in found)
        self.assertEqual(problems, [], "dangling first-party references:\n" + "\n".join(problems))

    def test_the_scanner_detects_a_planted_dangling_reference(self):
        """A scanner that quietly stopped matching looks exactly like a clean tree."""
        planted = "from marketflow.paths import runtime_dir as rd\nprint(rd.no_such_function())\n"
        self.assertEqual(len(find_dangling_references(planted, self.modules, self.packages)), 1)

    def test_the_scanner_respects_a_guarded_lookup(self):
        planted = ('from marketflow.paths import runtime_dir as rd\n'
                   'f = rd.maybe_absent() if hasattr(rd, "maybe_absent") else None\n')
        self.assertEqual(find_dangling_references(planted, self.modules, self.packages), [])


if __name__ == "__main__":
    unittest.main()
