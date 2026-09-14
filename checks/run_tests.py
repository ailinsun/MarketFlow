#!/usr/bin/env python3
"""Run every instrument self-test, offline help checks and aggregate verification."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Audit hooks record attempts even if an instrument catches the exception.
WRAPPER = '''import sys, runpy
attempts = []
def guard(event, args):
    if event.startswith("socket.") or event == "urllib.Request":
        attempts.append(event)
        raise RuntimeError("Network disabled by the offline test runner")
sys.addaudithook(guard)
sys.dont_write_bytecode = True
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


def run(path: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, "-B", "-c", WRAPPER, str(path), *args],cwd=ROOT,capture_output=True,text=True,timeout=30)
    if p.returncode != expected:
        raise AssertionError(path.name + ": " + " ".join(args) + " failed: " + p.stderr[-500:])
    return p


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


if __name__ == "__main__":
    main()
