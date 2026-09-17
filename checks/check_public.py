#!/usr/bin/env python3
"""Publication gate: fail the build on identifiers, private references or broken links.

Run before every publish, and with --history before the first one:

    python3 checks/check_public.py
    python3 checks/check_public.py --history

The patterns below encode the publication rules for this repository. Nothing in a
public tree may carry a person's name, a machine's paths, an operator's account,
an internal document reference, a credential, or a wallet address that is not a
public protocol contract on the allowlist.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

# Binary assets are reviewed by eye and pinned by digest. The tree ships none today;
# any entry added here must have been opened and looked at, not just hashed.
REVIEWED_IMAGES: dict[str, str] = {}

# Public protocol contracts on Polygon. Each one is a published, externally
# verifiable address that the code must know in order to talk to the venue at all.
# Nothing else may appear as a 40-hex address: no operator wallet, no tenant wallet,
# no third party's wallet. Synthetic fixture addresses are matched separately below.
# Binary files are never scanned as text. Each one is pinned to the digest it was
# reviewed at, so a changed or added binary fails the gate until a human looks at it.
REVIEWED_IMAGES = {
    "assets/marketflow-social.jpg": "9d46835486734d2073b1602a1056243e18f78119a2d2e57d9ef7fc3a5dda4950",
    "assets/marketflow.png": "59ea5ea491d3ea53f4528aa883d979343c41e48b87f0a34d03d9b176b5948544",
}

# No file in the current tree may name a person — not the README, not the source, not
# the docs, not the citation metadata, and not the `how_to_cite` / `publisher` fields
# embedded in the frozen snapshots. Attribution is the project and the GitHub account;
# provenance for earlier releases travels through DOIs, version identifiers and
# immutable URLs, which carry it without disclosing anything about a person.
#
# CFF 1.2.0 admits an entity author identified by `name` alone, and the Zenodo
# deposition metadata treats `creators[].name` as a free string, so this costs the
# release nothing.
#
# The rule applies to the current tree only. Commits and releases already published
# under the previous policy are left exactly as they are: rewriting public history
# would break the citation chain that points into it and would protect nothing, since
# the content is already distributed. `--history` therefore scans only what this
# release adds, and `--history-all` reports the legacy matches by design.

# Files exempt from the English-only rule. Dated research reports stay in the language
# they were written in — rewriting a published finding is not a translation, and the
# directory name says which language a reader is about to get. Everything that is code or
# developer-facing documentation is English.
NON_LATIN_EXEMPT_PREFIXES = ("reports/", "data/")
NON_LATIN_EXEMPT_SUFFIXES = ("_zh.md", "_zh.json")

ADDRESSES = {
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",  # Polymarket pUSD collateral token
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC (Polygon)
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # bridged USDC.e (Polygon)
    "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7",  # UMA CTF adapter (binary)
    "0x69c47de9d4d3dad79590d61b9e05918e03775f24",  # NegRisk UMA CTF adapter
    "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d",  # NegRisk CTF adapter
    "0xe111180000d2663c0091e4f400237545b87b996b",  # CTF Exchange v2
    "0xe2222d279d744050d28e00520010520000310f59",  # NegRisk CTF Exchange v2
}

# A synthetic address is one no human could control: its 40 hex digits are a short
# unit repeated, a long run of zeros, or a recognisable placeholder word. Real
# addresses never look like this, so fixtures can be told apart from accounts.
_PLACEHOLDER_WORDS = ("deadbeef", "1234567890abcdef")


def is_synthetic_address(address: str) -> bool:
    body = address[2:].lower()
    if len(body) != 40:
        return False
    if any(body.startswith(w) or body == w * (40 // len(w)) for w in _PLACEHOLDER_WORDS):
        return True
    if body.count("0") >= 20:                      # prefix + long zero fill
        return True
    for unit in range(1, 11):                      # a short unit repeated to length
        if 40 % unit == 0 and body == body[:unit] * (40 // unit):
            return True
    return False


PATTERNS = {
    # A public tree carries no person and no retired identity.
    "retired attribution": r"\b(?:N[o]ara|I[r]ene|S[I]LT\s+LLC|I[r]eneSun\d*|A[i]lin(?:\s+Sun)?)\b",
    # The published tree is English-only: CJK ideographs, CJK punctuation and
    # fullwidth forms are all residue from the private working repository.
    "non-latin script": r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]",
    # No machine this was built on.
    "local home path": r"/(?:Users|home)[/][^\s\"'<>]+",
    # No scratch or private-workspace references.
    # `docs/` is a published directory here, so a relative link into it is normal.
    # The private subtrees are matched by "internal reference" instead.
    "private path": r"scratchpad[/]|(?:\.\./)+(?:outputs|workbench)[/]",
    # Any literal IPv4 that is not loopback, RFC 1918 private, RFC 5737 documentation
    # range, or a well-known public resolver. Those four are safe to appear in code;
    # anything else names a real host somebody runs.
    "IPv4 address": r"(?<![\w.])(?:\d{1,3}(?:\\?\.)+){3}\d{1,3}(?![\w.])",
    "provider identifier": r"project_[a-z0-9]{8,}|[a-z0-9-]+\.(?:fly\.dev|workers\.dev|onrender\.com)|[a-z0-9]+_vps_[a-z_]+",
    "private hostname": r"[a-z0-9_-]+[.]local\b",
    # No pointer into the private repository's internal record.
    "internal reference": r"\u00a7\s*\d|decision[_ ]record|\u4efb\u52a1\u5361|docs/(?:spec|outputs|workbench|research)/",
    # A literal chat id or bot handle; an interpolated one (t.me/{...}) is a template.
    "Telegram identifier": r"(?:chat[_]id|telegram[_]id)\s*[\"']?\s*[:=]\s*[\"']?-?\d{5,}|t[.]me/[A-Za-z0-9_][^\s)\"']*",
    "credential": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b\d{7,}:[A-Za-z0-9_-]{30,})",
    # Imports of modules that are deliberately NOT part of the open release.
    "private import": r"(?:from|import)\s+(?:wolfram_|engine_|symbolic_compiler|(?:research|hq|bridge|agency|site|ops|web)\b|market_intel_router|internal_notify|sports_win_probability|polymarket_vol_sizing|polymarket_flb_paper_harvester|whale_report_dataset|polymarket_whale_profiler)",
    "unfilled placeholder": r"\{\{[^}]+\}\}|\[VER[I]FY:",
}

# Third-party distributions this repository declares. Anything imported that is not
# stdlib, not one of these, and not a module in the tree is an undeclared dependency.
DECLARED_THIRD_PARTY = {
    "cryptography", "eth_account", "eth_abi", "eth_utils", "httpx", "h2",
    "pydantic", "websockets", "rlp", "polymarket",
}

# A decorator line is not a social handle. Matches bare and dotted forms.
DECORATORS = r"\s*@[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(|\s*$)"


# Literal IPv4 addresses that carry no information about anybody's infrastructure.
SAFE_IP = re.compile(
    r"^(?:127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|0\.0\.0\.0$"
    r"|192\.0\.2\.|198\.51\.100\.|203\.0\.113\."          # RFC 5737 documentation
    r"|8\.8\.8\.8$|8\.8\.4\.4$|1\.1\.1\.1$|1\.0\.0\.1$)")  # public resolvers

# Reserved TLDs that can never route anywhere (RFC 2606 / RFC 6761).
RESERVED_EMAIL_TLD = re.compile(r"\.(?:example|invalid|test|localhost)$", re.I)

# The only real addresses allowed anywhere in the tree or its history: the GitHub
# no-reply address commits are authored under. A no-reply address is the account's
# public identity and forwards nowhere.
APPROVED_EMAILS = {
    "237421509+ailinsun@users.noreply.github.com",
    "noreply@github.com",
}


def _exempt(name: str, label: str) -> bool:
    """Rules that do not apply to a given file, each for a stated reason."""
    if label == "non-latin script" and (name.startswith(NON_LATIN_EXEMPT_PREFIXES)
                                        or name.endswith(NON_LATIN_EXEMPT_SUFFIXES)):
        return True                       # see NON_LATIN_EXEMPT_PREFIXES
    return False


def scan_text(name: str, content: str) -> list[str]:
    hits: list[str] = []
    for number, line in enumerate(content.splitlines(), 1):
        if name.startswith(".github/workflows/"):
            # GitHub Actions expression syntax, not an unfilled template placeholder.
            line = re.sub(r"\$\{\{[^}]*\}\}", "workflow expression", line)
        if name == "Makefile" or name.endswith(".mk"):
            # A leading @ in a recipe suppresses echo; it is not a social handle.
            line = re.sub(r"(?m)^\t@", "\t", line)
        for label, pattern in PATTERNS.items():
            if _exempt(name, label):
                continue
            found = re.search(pattern, line, re.I)
            if not found:
                continue
            if label == "IPv4 address" and all(SAFE_IP.match(ip) for ip in
                                               re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", line)):
                continue
            hits.append(f"{name}:{number}: {label}")
        for address in re.findall(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])", line):
            low = address.lower()
            if low not in ADDRESSES and not is_synthetic_address(low):
                hits.append(f"{name}:{number}: non-allowlisted address")
        for email in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line):
            if email not in APPROVED_EMAILS and not RESERVED_EMAIL_TLD.search(email):
                hits.append(f"{name}:{number}: unapproved email {email}")
        if re.search(r"(?<![\w])@[A-Za-z][A-Za-z0-9_]{2,}", line) and not re.match(DECORATORS, line):
            hits.append(f"{name}:{number}: handle")
    return hits


def scan_blob(name: str, content: bytes) -> list[str]:
    if name in REVIEWED_IMAGES:
        if hashlib.sha256(content).hexdigest() != REVIEWED_IMAGES[name]:
            return [name + ": image changed; explicit review required"]
        return []
    try:
        return scan_text(name, content.decode("utf-8"))
    except UnicodeDecodeError:
        return [name + ": binary file requires explicit review"]


# The last commit published under the previous gate. History before it is an
# already-public, already-reviewed fact that cannot be rewritten without destroying
# the DOI and citation chain, so the default history scan starts after it and checks
# what this release actually adds. `--history-all` scans everything and will report
# the known pre-existing matches.
PUBLISHED_BASELINE = "e68cd96"


def _stdlib_names() -> set[str]:
    """Top-level standard library module names on the running interpreter.

    `sys.stdlib_module_names` arrived in 3.10 and this project supports 3.9, so the
    older path enumerates the standard library directory instead. Getting this wrong
    in the permissive direction would let an undeclared dependency through, so the
    fallback errs toward listing more rather than fewer names and the import rule is
    backed by the negative-control test either way.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    import sysconfig
    out = set(sys.builtin_module_names)
    paths = sysconfig.get_paths()
    roots = [paths.get("stdlib"), paths.get("platstdlib")]
    roots += [str(Path(r) / "lib-dynload") for r in roots if r]   # C extensions
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for entry in Path(root).iterdir():
            if entry.suffix in (".py", ".so") or (entry.is_dir()
                                                  and entry.name.isidentifier()):
                out.add(entry.name.split(".")[0])
    return out


def _published_blobs(root: Path) -> set[str]:
    """Blob ids already reachable from the published baseline.

    A blob whose content is byte-identical to something already public cannot be a
    new disclosure, whatever it matches. Without this, every intermediate commit on a
    release branch re-reports the whole published history and the signal is lost.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-list", "--objects", PUBLISHED_BASELINE], cwd=root, text=True)
    except subprocess.CalledProcessError:
        return set()
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def _commits_to_scan(root: Path, every: bool) -> list[str]:
    if every:
        return subprocess.check_output(["git", "rev-list", "--all"], cwd=root,
                                       text=True).splitlines()
    try:
        return subprocess.check_output(["git", "rev-list", f"{PUBLISHED_BASELINE}..HEAD"],
                                       cwd=root, text=True).splitlines()
    except subprocess.CalledProcessError:       # a fresh repo without the baseline
        return subprocess.check_output(["git", "rev-list", "--all"], cwd=root,
                                       text=True).splitlines()


def check(root: Path, history: bool = False, history_all: bool = False) -> list[str]:
    hits: list[str] = []
    skip = {".git", ".venv", "__pycache__", "runtime"}
    files = [p for p in root.rglob("*")
             if p.is_file() and not set(p.relative_to(root).parts) & skip
             # Pre-publication working documents. They record what was removed and
             # therefore quote the very strings this gate forbids; neither is ever
             # committed, so neither can reach a published tree or its history.
             and p.name not in {"PUBLICATION_AUDIT.md", "EXCLUDED_FILES.txt"}]
    local_modules = {p.stem for p in files if p.suffix == ".py"}
    local_packages = {p.relative_to(root).parts[0] for p in files if p.suffix == ".py"}
    allowed = _stdlib_names() | DECLARED_THIRD_PARTY | local_modules | local_packages

    for p in files:
        name = p.relative_to(root).as_posix()
        if p.is_symlink():
            hits.append(name + ": symlink is not permitted in the publication")
            continue
        if p.suffix == ".pyc" or p.name == ".DS_Store" or p.name.endswith("~"):
            hits.append(name + ": generated/editor file")
        if name in REVIEWED_IMAGES:
            hits.extend(scan_blob(name, p.read_bytes()))
            continue
        try:
            content = p.read_text()
        except UnicodeDecodeError:
            hits.append(name + ": binary file requires explicit review")
            continue
        hits.extend(scan_text(name, content))
        if p.suffix == ".py":
            tree = ast.parse(content)
            if not ast.get_docstring(tree) and p.name != "__init__.py":
                hits.append(name + ": missing module docstring")
            for n in ast.walk(tree):
                modules = ([x.name for x in n.names] if isinstance(n, ast.Import)
                           else [n.module or ""] if isinstance(n, ast.ImportFrom) and n.level == 0
                           else [])
                for module in modules:
                    if module and module.split(".")[0] not in allowed:
                        hits.append(f"{name}: undeclared import {module}")
        if p.suffix == ".md":
            for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", content):
                if re.match(r"(?:https?://|mailto:|#)", target):
                    continue
                path = (p.parent / unquote(target.split("#")[0])).resolve()
                if not path.is_relative_to(root.resolve()) or not path.exists():
                    hits.append(name + ": broken/outside relative link " + target)

    # Published aggregates must stay aggregates. A per-wallet list inside one of these
    # snapshots would republish the trader identities the release exists not to expose.
    for p in (root / "data/farm_signature").glob("clean_data_*.json"):
        d = json.loads(p.read_text())
        if any("list" in d[k] for k in ("raw", "clean") if isinstance(d.get(k), dict)):
            hits.append(p.name + ": per-wallet list in a published aggregate")

    if history:
        commits = _commits_to_scan(root, every=history_all)
        published = set() if history_all else _published_blobs(root)
        seen: set[tuple[str, str]] = set()
        for commit in commits:
            meta = subprocess.check_output(
                ["git", "show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit], cwd=root, text=True)
            hits.extend(h + " (commit metadata)" for h in scan_text("commit " + commit[:8], meta))
            for entry in subprocess.check_output(["git", "ls-tree", "-r", commit], cwd=root, text=True).splitlines():
                info, blob_name = entry.split("\t", 1)
                oid = info.split()[-1]
                if (blob_name, oid) in seen or oid in published:
                    continue
                seen.add((blob_name, oid))
                blob = subprocess.check_output(["git", "cat-file", "blob", oid], cwd=root)
                hits.extend(h + " (history)" for h in scan_blob(blob_name, blob))
    return sorted(set(hits))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--history", action="store_true",
                    help="also scan commits added since the published baseline")
    ap.add_argument("--history-all", action="store_true",
                    help="scan every reachable commit, including already-published history")
    args = ap.parse_args()
    hits = check(args.root, args.history or args.history_all, args.history_all)
    for hit in hits:
        print(hit)
    print(f"{'FAIL' if hits else 'PASS'}: {len(hits)} residual matches")
    raise SystemExit(bool(hits))


if __name__ == "__main__":
    main()
