#!/usr/bin/env python3
"""Negative controls for the publication gate.

A scanner that has quietly stopped matching looks exactly like a clean repository.
These tests build a tree of deliberate violations, one per rule, and fail if any of
them gets through — so a rule that rots fails the suite instead of passing silently.

The positive control matters too: the gate must return nothing on the real tree, or
`make verify` would be green for the wrong reason.
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_public", ROOT / "checks/check_public.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

def _s(*parts: str) -> str:
    """Join fragments into a forbidden string at runtime.

    The fixtures below are assembled rather than written out, so this file does not
    itself contain the strings it is testing for. That keeps the gate free of
    exemptions: it scans every file in the tree including this one, and an exemption
    list is one more thing that can quietly grow.
    """
    return "".join(parts)


# (label the gate should report, a line that must trigger it)
VIOLATIONS = [
    ("retired attribution", _s("x = 'built at ", "No", "ara by ", "Ir", "ene of ", "SI", "LT LLC'")),
    ("retired attribution", _s("x = 'compiled by ", "Ai", "lin ", "Sun'")),
    ("non-latin script", _s("x = '", chr(0x4e2d), chr(0x6587), chr(0xff0c), chr(0x3002), "'")),
    ("local home path", _s("x = '/", "Users", "/somebody/dev/private/thing.py'")),
    ("private path", _s("x = '", "scratch", "pad/out.json'")),
    ("private path", _s("x = '../../", "work", "bench/notes.md'")),
    ("IPv4 address", _s("x = 'host 45.32.", "111.22'")),
    ("provider identifier", _s("x = '", "project", "_abcdefgh12'")),
    ("provider identifier", _s("x = 'shiny-thing.", "fly", ".dev'")),
    ("private hostname", _s("x = 'box.", "local", "'")),
    ("internal reference", _s("x = 'see \u00a7 512 of the ", "decision", "_record'")),
    ("internal reference", _s("x = '", "docs/", "outputs/security/review.md'")),
    ("Telegram identifier", _s("x = '", "chat", "_id: -1001234567'")),
    ("Telegram identifier", _s("x = 'join ", "t.me", "/somechannel'")),
    ("credential", _s("x = '", "gh", "p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012'")),
    ("credential", _s("x = '", "AK", "IAABCDEFGHIJKLMNOP'")),
    ("credential", _s("x = '-----BEGIN RSA ", "PRIVATE", " KEY-----'")),
    ("credential", _s("x = '1234567890", ":AAEabcdefghijklmnopqrstuvwxyz0123456789'")),
    ("private import", _s("import ", "wolfram", "_bridge")),
    ("private import", _s("from ", "hq", " import digest")),
    ("unfilled placeholder", _s("x = '{", "{FILL_ME}", "}'")),
    ("unfilled placeholder", _s("x = '[VER", "IFY: this claim]'")),
    ("non-allowlisted address", _s("x = '0x7f3ab29c1e05d84b6a", "0192fcd83be471a25c9e08'")),
    ("unapproved email", _s("x = 'mail me at somebody", "@", "gmail.com'")),
    ("handle", _s("x = 'ping ", "@", "someperson about it'")),
]


class TestGateCatchesViolations(unittest.TestCase):
    def test_every_rule_fires_on_its_own_violation(self):
        missed = []
        for label, line in VIOLATIONS:
            hits = gate.scan_text("probe.py", line)
            if not any(label in h for h in hits):
                missed.append((label, line, hits))
        self.assertEqual(missed, [], f"{len(missed)} violation(s) went undetected")

    def test_a_violating_file_fails_a_whole_tree_scan(self):
        """End to end, not just scan_text: the file walk must surface it too."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "leak.py").write_text(
                '"""Doc."""\nTOKEN = "' + _s("gh", "p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012") + '"\n')
            hits = gate.check(root)
            self.assertTrue(any("credential" in h for h in hits), hits)

    def test_generated_and_editor_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "stale.pyc").write_bytes(b"\x00")
            (root / ".DS_Store").write_bytes(b"\x00")
            hits = gate.check(root)
            self.assertEqual(sum("generated/editor file" in h for h in hits), 2, hits)

    def test_an_unreviewed_binary_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "mystery.bin").write_bytes(bytes(range(256)))
            hits = gate.check(root)
            self.assertTrue(any("binary file requires explicit review" in h for h in hits), hits)

    def test_a_changed_reviewed_image_is_refused(self):
        """Pinning by digest is the point: an edited asset must not pass silently."""
        self.assertTrue(gate.REVIEWED_IMAGES, "no reviewed images are pinned")
        name = next(iter(gate.REVIEWED_IMAGES))
        self.assertEqual(gate.scan_blob(name, b"not the reviewed bytes"),
                         [name + ": image changed; explicit review required"])


class TestGateAllowsWhatItShould(unittest.TestCase):
    def test_the_real_tree_is_clean(self):
        self.assertEqual(gate.check(ROOT), [])

    def test_synthetic_addresses_are_not_flagged(self):
        for addr in (_s("0xdeadbeef0011223344", "5566778899aabbccddeeff"),
                     "0x" + "11" * 20,
                     "0x" + "00" * 20):
            self.assertEqual(gate.scan_text("f.py", f'x = "{addr}"'), [], addr)

    def test_allowlisted_protocol_contracts_are_not_flagged(self):
        for addr in gate.ADDRESSES:
            self.assertEqual(gate.scan_text("f.py", f'x = "{addr}"'), [], addr)

    def test_reserved_tld_emails_are_not_flagged(self):
        for email in ("dev@example.invalid", "a@b.test", "x@y.localhost"):
            self.assertEqual(gate.scan_text("f.py", f'x = "{email}"'), [], email)

    def test_decorators_are_not_social_handles(self):
        at = "@"
        for line in (at + "dataclass", "    " + at + "property",
                     at + "functools.lru_cache(maxsize=8)"):
            self.assertEqual(gate.scan_text("f.py", line), [], line)

    def test_no_file_may_name_a_person_not_even_citation_metadata(self):
        """There is no exemption. A rule with a carve-out grows one, so there is none.

        Citation metadata, the frozen snapshots' embedded blocks and the package
        metadata are all covered: provenance travels through DOIs and immutable URLs,
        which carry it without disclosing a person.
        """
        person = _s("Ai", "lin ", "Sun")
        for name in ("CITATION.cff", ".zenodo.json", "README.md",
                     "data/zero_sum_ledger/zero_sum_ledger.json",
                     "data/README.md", "pyproject.toml", "marketflow/risk/exposure.py"):
            self.assertNotEqual(gate.scan_text(name, f'x = "{person}"'), [],
                                f"{name} did not refuse a personal name")
        cff = _s("authors:\n  - family-names: ", "Sun", "\n    given-names: ", "Ai", "lin")
        self.assertNotEqual(gate.scan_text("CITATION.cff", cff), [])

    def test_entity_attribution_is_accepted(self):
        """What replaced it has to pass, or the rule is unusable."""
        for line in ('  - name: "MarketFlow contributors"',
                     '"publisher": "MarketFlow contributors"',
                     '{"name": "MarketFlow contributors"}'):
            self.assertEqual(gate.scan_text("CITATION.cff", line), [], line)

    def test_dated_reports_keep_their_original_language(self):
        cjk = "x = '" + chr(0x4e2d) + chr(0x6587) + "'"
        self.assertEqual(gate.scan_text("reports/zh/report.md", cjk), [])
        self.assertNotEqual(gate.scan_text("marketflow/risk/x.py", cjk), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
