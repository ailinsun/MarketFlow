#!/usr/bin/env python3
"""Run every self-test in the repository with the network switched off.

Two families are covered by the same runner, under the same audit hook:

  * `instruments/` and `checks/`, invoked as scripts, plus the frozen-aggregate
    verification and the synthetic end-to-end input flows;
  * the `marketflow` package, invoked as modules.

The audit hook records a socket or urllib attempt even when the module under test
catches the exception, so "the self-test passed" cannot quietly mean "it reached
the internet and got an answer it liked".

A package module whose optional third-party dependency is not installed is
reported as skipped rather than failed: the core is standard-library-only by
design, and the signing layer is an opt-in install. Only the packages named in
requirements-signing.txt count as optional. Any other missing module -- above
all a missing or misnamed first-party module -- is a failure, so a broken import
can never be laundered into "skipped".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Audit hooks record attempts even if an instrument catches the exception.
_GUARD = '''import sys
attempts = []
def guard(event, args):
    if event.startswith("socket.") or event == "urllib.Request":
        attempts.append(event)
        raise RuntimeError("Network disabled by the offline test runner")
sys.addaudithook(guard)
sys.dont_write_bytecode = True
'''

WRAPPER = _GUARD + '''import runpy
sys.argv = sys.argv[1:]
sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[0]).parent))
code = 0
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    code = exc.code or 0
if attempts:
    raise SystemExit("Network attempt during offline test")
raise SystemExit(code)
'''

# Import names of the optional signing install (requirements-signing.txt). A
# missing module outside this set fails the run.
OPTIONAL_IMPORTS = ("cryptography", "eth_account", "eth_abi", "eth_utils",
                    "polymarket", "httpx", "h2", "pydantic", "websockets")

MODULE_WRAPPER = _GUARD + "OPTIONAL = " + repr(OPTIONAL_IMPORTS) + "\n" + '''import runpy
module = sys.argv[1]
sys.argv = sys.argv[1:]
code = 0
try:
    runpy.run_module(module, run_name="__main__", alter_sys=True)
except SystemExit as exc:
    code = exc.code or 0
except ModuleNotFoundError as exc:
    top = (exc.name or "").split(".")[0]
    if top in OPTIONAL:
        print("MISSING_DEPENDENCY:" + (exc.name or "?"))
        raise SystemExit(0)
    raise
if attempts:
    raise SystemExit("Network attempt during offline test")
raise SystemExit(code)
'''


def run(path: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, "-B", "-c", WRAPPER, str(path), *args],cwd=ROOT,capture_output=True,text=True,timeout=120)
    if p.returncode != expected:
        raise AssertionError(path.name + ": " + " ".join(args) + " failed: " + p.stderr[-500:])
    return p


def run_module(module: str, *args: str) -> tuple[str, str]:
    """Run one package module's self-test. Returns (status, detail)."""
    p = subprocess.run([sys.executable, "-B", "-c", MODULE_WRAPPER, module, *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    if "MISSING_DEPENDENCY:" in p.stdout:
        name = p.stdout.split("MISSING_DEPENDENCY:", 1)[1].split()[0]
        return "SKIP", f"needs {name} (pip install -r requirements-signing.txt)"
    if p.returncode != 0:
        return "FAIL", (p.stderr or p.stdout)[-700:]
    return "PASS", ""


def package_selftest_modules() -> list[str]:
    """Every marketflow module that declares a --selftest entry point."""
    out = []
    for path in sorted((ROOT / "marketflow").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if "--selftest" in path.read_text():
            out.append(str(path.relative_to(ROOT).with_suffix("")).replace("/", "."))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    scripts = sorted(ROOT.glob("instruments/*.py")) + sorted(ROOT.glob("checks/*.py"))
    for p in scripts:
        run(p,"--help")
        print("PASS offline help:",p.relative_to(ROOT))
    for p in sorted(ROOT.glob("instruments/*.py")):
        if '"--selftest"' in p.read_text():
            run(p,"--selftest")
            print("PASS self-test:",p.name)
    run(ROOT/"instruments/verify_snapshots.py")
    print("PASS frozen aggregate verification")
    for name in ("predmkt_zero_sum_ledger.py","farm_rank_audit.py"):
        result=run(ROOT/"instruments"/name,expected=2)
        assert "not distributed" in result.stderr
    run(ROOT/"instruments/polymarket_farm_filter.py",expected=1)
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp)
        meta={"m":{"closed":True,"clob_token_ids":["yes"],"outcome_prices":[1]},"open":{"closed":False},"no_fee":{"closed":True,"clob_token_ids":["yes"],"outcome_prices":[0]}}
        rows=[{"cid":"m","asset":"yes","side":"BUY","px":.5,"sz":10,"ts":1000}]
        for delta in ({"side":"SELL"},{"cid":"missing"},{"cid":"open"},{"cid":"no_fee"},{"px":0}):
            rows.append({**rows[0],**delta})
        (d/"tape.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        (d/"meta.json").write_text(json.dumps(meta))
        (d/"fees.json").write_text(json.dumps({"m":{"rate":.05}}))
        out=run(ROOT/"instruments/predmkt_zero_sum_ledger.py","--tape",str(d/"tape.jsonl"),"--meta",str(d/"meta.json"),"--fees",str(d/"fees.json"))
        ledger=json.loads(out.stdout)
        assert ledger["usd"]["taker_net"]==4.88
        assert ledger["sample"]["trades_used"]==1
        assert set(ledger["sample"]["dropped_trades"].values())=={1}
        (d/"empty.jsonl").write_text("")
        (d/"farm.json").write_text(json.dumps({"farm_wallets": []}))
        result = run(ROOT/"instruments/farm_rank_audit.py", "--tape", str(d/"empty.jsonl"), "--farm", str(d/"farm.json"), "--out", str(d/"rank.json"), expected=2)
        assert "No usable BUY fills" in result.stderr
        assert not (d/"rank.json").exists()
        feeds=[{"wallet":f"synthetic-{i}","wallet_label":"synthetic","condition_id":"example","side":"BUY","price":.999,"size":100,"ts_source_ms":1000} for i in range(5) for _ in range(4)]
        (d/"feed.jsonl").write_text("\n".join(json.dumps(r) for r in feeds))
        out=run(ROOT/"instruments/polymarket_farm_filter.py","--path",str(d/"feed.jsonl"))
        summary=json.loads(out.stdout)
        assert summary["farm_wallets"]==5
        assert "synthetic" not in out.stdout and "old_list" not in out.stdout and "new_list" not in out.stdout
    print("PASS end-to-end input handling, drop accounting and aggregate-only farm output")

    failures, skipped = [], []
    for module in package_selftest_modules():
        status, detail = run_module(module, "--selftest")
        print(f"{status} package self-test: {module}" + (f"  [{detail}]" if detail else ""))
        if status == "FAIL":
            failures.append((module, detail))
        elif status == "SKIP":
            skipped.append(module)

    # The signing layer keeps its suites in dedicated runners rather than a
    # --selftest flag, because they need the optional third-party install.
    for module in ("marketflow.guardian.selftest",):
        status, detail = run_module(module)
        print(f"{status} suite: {module}" + (f"  [{detail}]" if detail else ""))
        if status == "FAIL":
            failures.append((module, detail))
        elif status == "SKIP":
            skipped.append(module)

    unit = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                           "-t", "."], cwd=ROOT, capture_output=True, text=True, timeout=300)
    if unit.returncode != 0:
        failures.append(("tests/", unit.stderr[-700:]))
        print("FAIL unit tests (tests/)")
    else:
        print("PASS unit tests (tests/): " + unit.stderr.strip().splitlines()[-1])

    demo = ROOT / "examples/portfolio_risk_demo.py"
    if demo.exists():
        run(demo)
        print("PASS runnable demonstration (examples/portfolio_risk_demo.py)")

    if failures:
        for module, detail in failures:
            print(f"\n--- {module} ---\n{detail}", file=sys.stderr)
        raise SystemExit(f"{len(failures)} package self-test(s) failed")
    if skipped:
        print(f"\nNOTE {len(skipped)} module(s) skipped for an uninstalled optional "
              f"dependency: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
