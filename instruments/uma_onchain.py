"""Read UMA proposal state with public eth_call and test the decoder offline.

UMA Optimistic Oracle 链上只读层 (纯 stdlib) — settlement guard 的 proposal 数据源。

Gamma 的 `umaResolutionStatuses` 只给状态 token ("proposed"/"disputed"), 不给 proposed
outcome 方向。方向只在链上: Polymarket UMA adapter 的 questions(questionID) 存
(requestTimestamp, ancillaryData), 用它们向 OptimisticOracleV2.getRequest 查
proposedPrice (YES_OR_NO_QUERY 语义: 1e18 = outcomes[0], 0 = outcomes[1],
0.5e18 = 50/50 无法裁定)。OO 地址不硬编码 — 从 adapter.optimisticOracle() 动态读。

negRisk 市场的 UMA 请求键是 gamma `negRiskRequestID` (整组一个请求), 普通二元市场
用 `questionID`; 本模块两个都试, 以链上有记录者为准。

方向映射与 Request 结构布局于 2026-07-03 用两个方向相反的真实 proposed 市场双源
验证 (链上 proposedPrice 与 gamma outcomePrices 收敛方向一致, state 与
umaResolutionStatuses 一致)。

keccak256 无第三方库可用, 内置纯 Python Keccak-f[1600]; 自验零记忆依赖: 同一
permutation 换 SHA3 padding 必须复现 hashlib.sha3_256, 另加以太坊空哈希已知值。

隔离硬边界 (同包约定): 只读 eth_call, 零 secret, 零 live-stack import, fail-soft —
任何网络 / 解码失败返回 None, 由调用方诚实降级 (提醒文案不写拿不到的方向)。
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Any, Callable, Optional

SCHEMA_VERSION = "marketflow-alerts-uma-onchain-v0.1"

# 公共 Polygon RPC (主 + 备)。直连: 空 ProxyHandler 覆盖 *_proxy 环境变量 (同包约定,
# 绝不耦合 VPN proxy)。
POLYGON_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
)
_UA = "MarketFlow-Alerts/0.1 (+read-only UMA settlement monitor)"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_TIMEOUT = 15.0

# UMA OOv2 State enum (OptimisticOracleV2Interface.State)。
STATE_LABELS = {
    0: "invalid",
    1: "requested",
    2: "proposed",
    3: "expired",
    4: "disputed",
    5: "resolved",
    6: "settled",
}

# YES_OR_NO_QUERY 的 too-early magic value (type(int256).min): 提案人声明"现在还
# 无法回答", 无方向。
_TOO_EARLY = -(2 ** 255)


# ---------------------------------------------------------------- keccak256


_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(a: list[list[int]]) -> list[list[int]]:
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= _RC[rnd]
    return a


def _sponge256(data: bytes, pad_byte: int) -> bytes:
    rate = 136
    a = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(pad_byte)
    while len(padded) % rate:
        padded.append(0x00)
    padded[-1] |= 0x80
    for block_ofs in range(0, len(padded), rate):
        block = padded[block_ofs:block_ofs + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        a = _keccak_f(a)
    out = b""
    for y in range(5):
        for x in range(5):
            out += a[x][y].to_bytes(8, "little")
            if len(out) >= 32:
                return out[:32]
    return out[:32]


def keccak256(data: bytes) -> bytes:
    return _sponge256(data, 0x01)


def _keccak_selfcheck() -> None:
    for t in (b"", b"abc", b"hello world", bytes(range(200))):
        if _sponge256(t, 0x06) != hashlib.sha3_256(t).digest():
            raise RuntimeError("keccak permutation self-check failed")
    if keccak256(b"").hex() != "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470":
        raise RuntimeError("keccak256 padding self-check failed")


_keccak_selfcheck()


def _selector(signature: str) -> str:
    return keccak256(signature.encode()).hex()[:8]


SEL_QUESTIONS = _selector("questions(bytes32)")                                  # 95addb90
SEL_OPTIMISTIC_ORACLE = _selector("optimisticOracle()")
SEL_GET_REQUEST = _selector("getRequest(address,bytes32,uint256,bytes)")         # a9904f9b
SEL_GET_STATE = _selector("getState(address,bytes32,uint256,bytes)")             # ba4b930c

# bytes32("YES_OR_NO_QUERY") 左对齐 — Polymarket 全部市场用这一个 identifier。
IDENTIFIER_YES_OR_NO = "YES_OR_NO_QUERY".encode().hex().ljust(64, "0")


# ---------------------------------------------------------------- eth_call


class OnchainError(Exception):
    """RPC / 解码失败 (调用方 fail-soft)。"""


def eth_call(
    to: str,
    data: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    rpcs: tuple[str, ...] = POLYGON_RPCS,
) -> str:
    """只读 eth_call, 依次试每个公共 RPC; 全失败抛 OnchainError。"""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }).encode()
    last = ""
    for rpc in rpcs:
        try:
            req = urllib.request.Request(
                rpc, data=payload,
                headers={"User-Agent": _UA, "Content-Type": "application/json"},
            )
            with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
            if isinstance(body, dict) and isinstance(body.get("result"), str):
                return body["result"]
            last = str(body.get("error") if isinstance(body, dict) else body)[:200]
        except Exception as exc:  # noqa: BLE001 - 逐 RPC fail-over
            last = f"{type(exc).__name__}: {exc}"
    raise OnchainError(f"eth_call {to} failed on all RPCs: {last}")


def _words(raw_hex_result: str) -> list[str]:
    body = raw_hex_result[2:] if raw_hex_result.startswith("0x") else raw_hex_result
    return [body[i:i + 64] for i in range(0, len(body), 64)]


def _int256(word: str) -> int:
    v = int(word, 16)
    return v - 2 ** 256 if v >= 2 ** 255 else v


def _hex32(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


# ---------------------------------------------------------------- UMA reads


_ORACLE_CACHE: dict[str, str] = {}


def _oracle_of(adapter: str, *, timeout: float, call: Callable[..., str]) -> str:
    """adapter.optimisticOracle() — 动态发现, 进程内缓存。"""
    key = adapter.lower()
    if key not in _ORACLE_CACHE:
        raw = call(adapter, "0x" + SEL_OPTIMISTIC_ORACLE, timeout=timeout)
        addr = "0x" + raw[-40:]
        if int(addr, 16) == 0:
            raise OnchainError(f"adapter {adapter} returned zero oracle address")
        _ORACLE_CACHE[key] = addr
    return _ORACLE_CACHE[key]


def _question_data(adapter: str, request_key: str, *, timeout: float, call: Callable[..., str]) -> Optional[tuple[int, str]]:
    """adapter.questions(key) → (requestTimestamp, ancillaryData hex) 或 None (键无记录)。

    QuestionData 布局 (UmaCtfAdapter / NegRiskUmaCtfAdapter, 2026-07-03 实测,
    reward/bond/rewardToken 字段与 gamma umaReward/umaBond/USDC 对上验证):
    word[0]=requestTimestamp, word[11]=ancillaryData offset。
    """
    raw = call(adapter, "0x" + SEL_QUESTIONS + request_key[2:], timeout=timeout)
    w = _words(raw)
    if len(w) < 13:
        return None
    ts = int(w[0], 16)
    if not (1_400_000_000 < ts < 4_000_000_000):  # 无记录时全零; 异常布局 fail-soft
        return None
    body = raw[2:]
    anc_ofs = int(w[11], 16)
    if anc_ofs <= 0 or anc_ofs % 32 or anc_ofs * 2 + 64 > len(body):
        return None
    anc_len = int(body[anc_ofs * 2: anc_ofs * 2 + 64], 16)
    anc_hex = body[anc_ofs * 2 + 64: anc_ofs * 2 + 64 + anc_len * 2]
    if len(anc_hex) != anc_len * 2:
        return None
    return ts, anc_hex


def fetch_uma_proposal(
    resolved_by: str,
    question_id: Optional[str] = None,
    neg_risk_request_id: Optional[str] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    call: Callable[..., str] = eth_call,
) -> Optional[dict[str, Any]]:
    """读一个市场当前 UMA 请求的链上状态。返回 dict 或 None (无记录/任何失败)。

    resolved_by          : gamma `resolvedBy` (发起 UMA 请求的 adapter)
    question_id          : gamma `questionID`
    neg_risk_request_id  : gamma `negRiskRequestID` (negRisk 市场的真正请求键, 先试)

    返回:
      state / state_label      : OOv2 State enum (2=proposed, 4=disputed, …)
      proposed_outcome_index   : 0 (=outcomes[0]) / 1 (=outcomes[1]) / None
      proposed_outcome_kind    : "outcome0" | "outcome1" | "split_50_50" |
                                 "too_early" | "none"
      proposer / disputer / disputed / settled / expiration_time
    """
    adapter = str(resolved_by or "").strip().lower()
    if not adapter.startswith("0x") or len(adapter) != 42:
        return None
    keys = [k for k in (neg_risk_request_id, question_id)
            if k and str(k).strip().lower().startswith("0x") and len(str(k).strip()) == 66]
    try:
        qd = None
        used_key = None
        for key in keys:
            qd = _question_data(adapter, str(key).strip().lower(), timeout=timeout, call=call)
            if qd is not None:
                used_key = str(key).strip().lower()
                break
        if qd is None:
            return None
        ts, anc_hex = qd
        oracle = _oracle_of(adapter, timeout=timeout, call=call)

        pad = (32 - len(anc_hex) // 2 % 32) % 32
        args = (
            adapter[2:].rjust(64, "0")
            + IDENTIFIER_YES_OR_NO
            + _hex32(ts)
            + _hex32(0x80)
            + _hex32(len(anc_hex) // 2)
            + anc_hex + "0" * (pad * 2)
        )
        state = int(call(oracle, "0x" + SEL_GET_STATE + args, timeout=timeout), 16)
        rw = _words(call(oracle, "0x" + SEL_GET_REQUEST + args, timeout=timeout))
        if state == 0 or len(rw) < 16:
            return None

        # OOv2 Request 布局: [0]=proposer [1]=disputer [3]=settled
        # [4..10]=requestSettings [11]=proposedPrice [12]=resolvedPrice
        # [13]=expirationTime (2026-07-03 双市场方向验证)。
        proposed_price = _int256(rw[11])
        disputer = "0x" + rw[1][-40:]
        disputed = int(disputer, 16) != 0 or state == 4

        if state in (1,):  # requested: 还没有 proposal
            kind, index = "none", None
        elif proposed_price == 10 ** 18:
            kind, index = "outcome0", 0
        elif proposed_price == 0:
            kind, index = "outcome1", 1
        elif proposed_price == 5 * 10 ** 17:
            kind, index = "split_50_50", None
        elif proposed_price == _TOO_EARLY:
            kind, index = "too_early", None
        else:
            kind, index = "unknown", None

        return {
            "schema_version": SCHEMA_VERSION,
            "adapter": adapter,
            "oracle": oracle,
            "request_key": used_key,
            "request_timestamp": ts,
            "state": state,
            "state_label": STATE_LABELS.get(state, f"unknown_{state}"),
            "proposed_price_e18": proposed_price,
            "proposed_outcome_kind": kind,
            "proposed_outcome_index": index,
            "proposer": "0x" + rw[0][-40:],
            "disputer": disputer if int(disputer, 16) != 0 else None,
            "disputed": disputed,
            "settled": int(rw[3], 16) != 0,
            "expiration_time": int(rw[13], 16),
        }
    except (OnchainError, ValueError):
        return None


# ---------------------------------------------------------------- selftest


def selftest() -> dict[str, Any]:
    """离线自测 (stub transport, 零网络)。"""
    checks: dict[str, bool] = {}

    checks["keccak_sha3_cross"] = _sponge256(b"marketflow", 0x06) == hashlib.sha3_256(b"marketflow").digest()
    checks["selector_questions"] = SEL_QUESTIONS == "95addb90"
    checks["selector_get_request"] = SEL_GET_REQUEST == "a9904f9b"
    checks["selector_get_state"] = SEL_GET_STATE == "ba4b930c"
    checks["identifier_shape"] = IDENTIFIER_YES_OR_NO.startswith("5945535f4f525f4e4f5f5155455259") and len(IDENTIFIER_YES_OR_NO) == 64

    # stub 链: 复刻 2026-07-03 实测的两个真实市场形状 (方向相反)。
    ANC = b"q: title: stub market, description: stub"
    anc_hex = ANC.hex()
    ts = 1_782_878_576

    def _qdata_result() -> str:
        head = [_hex32(ts)] + [_hex32(0)] * 10 + [_hex32(12 * 32)]
        pad = (32 - len(ANC) % 32) % 32
        return "0x" + "".join(head) + _hex32(len(ANC)) + anc_hex + "0" * (pad * 2)

    def _request_result(price: int, disputer: int = 0) -> str:
        w = [_hex32(0)] * 16
        w[0] = _hex32(0xAAAA)
        w[1] = _hex32(disputer)
        w[11] = _hex32(price % 2 ** 256)
        w[13] = _hex32(ts + 7200)
        return "0x" + "".join(w)

    scenarios = {"state": 2, "price": 10 ** 18, "disputer": 0}

    def stub_call(to: str, data: str, *, timeout: float = 0) -> str:
        sel = data[2:10]
        if sel == SEL_QUESTIONS:
            # 只有 negrisk 键有记录, questionID 键返回全零 (证明键 fallback 顺序)
            if data[10:74] == "ab" * 32:
                return _qdata_result()
            return "0x" + _hex32(0) * 13
        if sel == SEL_OPTIMISTIC_ORACLE:
            return "0x" + _hex32(0xBEEF)
        if sel == SEL_GET_STATE:
            return "0x" + _hex32(scenarios["state"])
        if sel == SEL_GET_REQUEST:
            return _request_result(scenarios["price"], scenarios["disputer"])
        raise OnchainError(f"unexpected selector {sel}")

    adapter = "0x" + "69" * 20
    good_key = "0x" + "ab" * 32
    dead_key = "0x" + "cd" * 32

    # proposed → outcome0
    out = fetch_uma_proposal(adapter, question_id=dead_key, neg_risk_request_id=good_key, call=stub_call)
    checks["proposed_outcome0"] = (
        out is not None and out["state_label"] == "proposed"
        and out["proposed_outcome_index"] == 0 and not out["disputed"]
        and out["request_key"] == good_key
    )

    # proposed → outcome1 (price 0)
    scenarios.update(price=0)
    out = fetch_uma_proposal(adapter, question_id=good_key, call=stub_call)
    checks["proposed_outcome1"] = out is not None and out["proposed_outcome_index"] == 1

    # disputed
    scenarios.update(state=4, disputer=0xD15)
    out = fetch_uma_proposal(adapter, question_id=good_key, call=stub_call)
    checks["disputed_flag"] = out is not None and out["disputed"] and out["state_label"] == "disputed"

    # 50/50
    scenarios.update(state=2, price=5 * 10 ** 17, disputer=0)
    out = fetch_uma_proposal(adapter, question_id=good_key, call=stub_call)
    checks["split_50_50"] = out is not None and out["proposed_outcome_kind"] == "split_50_50" and out["proposed_outcome_index"] is None

    # too-early magic value
    scenarios.update(price=_TOO_EARLY)
    out = fetch_uma_proposal(adapter, question_id=good_key, call=stub_call)
    checks["too_early"] = out is not None and out["proposed_outcome_kind"] == "too_early"

    # 键无记录 → None
    out = fetch_uma_proposal(adapter, question_id=dead_key, call=stub_call)
    checks["no_record_none"] = out is None

    # 网络故障 → None (fail-soft)
    def boom_call(to: str, data: str, *, timeout: float = 0) -> str:
        raise OnchainError("network down")

    checks["network_error_none"] = fetch_uma_proposal(adapter, question_id=good_key, call=boom_call) is None

    # 坏 adapter 入参 → None
    checks["bad_adapter_none"] = fetch_uma_proposal("not-an-address", question_id=good_key, call=stub_call) is None

    ok = all(checks.values())
    return {"schema_version": SCHEMA_VERSION + "-selftest", "PASS": ok,
            "checks": checks, "failed": [k for k, v in checks.items() if not v]}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="UMA on-chain proposal reader (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--resolved-by", help="live: adapter address (gamma resolvedBy)")
    ap.add_argument("--question-id", help="live: gamma questionID")
    ap.add_argument("--neg-risk-request-id", help="live: gamma negRiskRequestID")
    args = ap.parse_args()
    if args.selftest:
        rep = selftest()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        raise SystemExit(0 if rep["PASS"] else 1)
    if args.resolved_by:
        print(json.dumps(
            fetch_uma_proposal(args.resolved_by, args.question_id, args.neg_risk_request_id),
            ensure_ascii=False, indent=2))
        raise SystemExit(0)
    ap.print_help()
