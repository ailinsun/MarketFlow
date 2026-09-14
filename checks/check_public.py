#!/usr/bin/env python3
"""Check public files for identifiers, private references, broken links and imports."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REVIEWED_IMAGES = {
    "assets/marketflow-social.jpg": "3dc37282df8e99684208ec563b0b7b4194d2822f3b64156c5ea32d51afdbe59b",
    "assets/marketflow.png": "3a17ac7d2184d746583c1611f2a1eb06e5ce66ba131347d379bc9e49169fe9b0",
}
ADDRESSES = {
    "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7",
    "0x69c47de9d4d3dad79590d61b9e05918e03775f24",
    "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d",
}
PATTERNS = {
    "retired attribution": r"\b(?:N[o]ara|I[r]ene|S[I]LT\s+LLC)\b",
    "local home path": r"/(?:Users|home)[/][^\s\"'<>]+",
    "private path": r"(?:runtime|scratchpad)[/]|(?:\.\./)+(?:docs|outputs|workbench)[/]",
    "IPv4 address": r"(?<![\w.])(?:\d{1,3}(?:\\?\.)+){3}\d{1,3}(?![\w.])",
    "provider identifier": r"project_[a-z0-9]{8,}|[a-z0-9-]+\.(?:fly\.dev|workers\.dev|onrender\.com)|[a-z0-9]+_vps_[a-z_]+",
    "private hostname": r"[a-z0-9_-]+[.]local\b",
    "internal reference": r"\u00a7\s*\d+|decision[_]record|\u4efb\u52a1\u5361",
    "Telegram identifier": r"(?:chat[_]id|telegram[_]id)\s*[\"']?\s*[:=]\s*[\"']?-?\d{5,}|t[.]me/[^\s)]+",
    "credential": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b\d{7,}:[A-Za-z0-9_-]{30,})",
    "private import": r"(?:from|import)\s+(?:trade[.]|wolfram_|engine_|symbolic_compiler|research[.]|alerts[.])",
    "unfilled placeholder": r"\{\{[^}]+\}\}|\u005bVERIFY:",
}


def scan_text(name: str, content: str) -> list[str]:
    hits = []
    for number, line in enumerate(content.splitlines(), 1):
        if name.startswith(".github/workflows/"):
            line = re.sub(r"\$\{\{ matrix\.python \}\}", "matrix value", line)
        for label, pattern in PATTERNS.items():
            if re.search(pattern, line, re.I):
                hits.append(f"{name}:{number}: {label}")
        for address in re.findall(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])", line):
            if address.lower() not in ADDRESSES:
                hits.append(f"{name}:{number}: non-allowlisted address")
        for email in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",line):
            if email not in {"237421509+ailinsun@users.noreply.github.com", "noreply@github.com"}:
                hits.append(f"{name}:{number}: unapproved email")
        without_email = line.replace("237421509+ailinsun@users.noreply.github.com", "").replace("noreply@github.com", "")
        without_email = re.sub(r"(?<=@)[0-9a-f]{40}\b", "", without_email)
        if re.search(r"(?<![\w])@[A-Za-z][A-Za-z0-9_]{2,}", without_email) and not re.match(r"\s*@(?:dataclass|staticmethod|classmethod|property)\b", line):
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


def check(root: Path, history: bool = False) -> list[str]:
    hits = []
    files = [p for p in root.rglob("*") if p.is_file() and not set(p.relative_to(root).parts).intersection({".git", ".venv", "__pycache__"}) and p.name != "PUBLISH_REPORT.md"]
    local_modules = {p.stem for p in files if p.suffix == ".py"}
    allowed_imports = {"__future__", "argparse", "ast", "collections", "dataclasses", "datetime", "gzip", "hashlib", "json", "math", "os", "pathlib", "re", "runpy", "subprocess", "sys", "tempfile", "typing", "unittest", "urllib"}
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
        try: content = p.read_text()
        except UnicodeDecodeError:
            hits.append(name + ": binary file requires explicit review")
            continue
        hits.extend(scan_text(name, content))
        if p.suffix == ".py":
            tree = ast.parse(content)
            doc = ast.get_docstring(tree)
            if not doc or not re.search(r"[A-Za-z]{3,}",doc.splitlines()[0]):
                hits.append(name + ": missing English module summary")
            for n in ast.walk(tree):
                modules = [x.name for x in n.names] if isinstance(n,ast.Import) else [n.module or ""] if isinstance(n,ast.ImportFrom) else []
                for module in modules:
                    if module.split('.')[0] not in allowed_imports | local_modules:
                        hits.append(name + ": undeclared import " + module)
        if p.suffix == ".md":
            for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)",content):
                if re.match(r"(?:https?://|mailto:|#)",target): continue
                path = (p.parent / unquote(target.split('#')[0])).resolve()
                if not path.is_relative_to(root.resolve()) or not path.exists():
                    hits.append(name + ": broken/outside relative link")
    for p in (root / "data/farm_signature").glob("clean_data_*.json"):
        d = json.loads(p.read_text())
        if any("list" in d[k] for k in ("raw", "clean")):
            hits.append(p.name + ": per-wallet list")
    if history:
        commits = subprocess.check_output(["git", "rev-list", "--all"], cwd=root, text=True).splitlines()
        seen = set()
        for commit in commits:
            meta = subprocess.check_output(["git", "show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit],cwd=root,text=True)
            hits.extend(scan_text("commit metadata",meta))
            entries = subprocess.check_output(["git", "ls-tree", "-r", commit],cwd=root,text=True).splitlines()
            for entry in entries:
                info,name = entry.split('\t',1)
                oid=info.split()[-1]
                if (name, oid) in seen: continue
                seen.add((name, oid))
                content = subprocess.check_output(["git","cat-file","blob",oid],cwd=root)
                hits.extend(hit + " (history)" for hit in scan_blob(name, content))
    return sorted(set(hits))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root",type=Path,default=ROOT)
    ap.add_argument("--history",action="store_true")
    args = ap.parse_args()
    hits = check(args.root,args.history)
    for hit in hits: print(hit)
    print(f"{'FAIL' if hits else 'PASS'}: {len(hits)} residual matches")
    raise SystemExit(bool(hits))


if __name__ == "__main__":
    main()
