#!/usr/bin/env python3
"""Compare event-contract fee curves and solve revenue-neutral alternatives.

事件合约费率几何：纯代数求解加上历史样本的分布标定常数。
Functions accept an explicit distribution; uniform_dist() supplies a synthetic
example. The default table uses historical aggregate calibration constants,
not the later frozen ledger snapshot. Its original tape is not distributed.
Revenue neutrality holds with the assumed distribution fixed; changes in
trading behaviour are scenarios, not measured outcomes. Venue fee schedules
are historical assumptions rather than a statement of current pricing.
"""
from __future__ import annotations

import argparse

# ── ① 纯代数层 (零数据依赖) ──────────────────────────────────────────────

def fee_hump(p: float, r: float) -> float:
    """现行每股费: r·p(1−p)。Polymarket r 实测 0.05; Kalshi ≈0.07 (另有整分向上取整, 口径未核实, 此处不建模)。"""
    return r * p * (1.0 - p)


def fee_floored(p: float, r: float, m: float) -> float:
    """替代曲线: r·p·max(1−p, m)。m=0 退化回现行驼峰。"""
    return r * p * max(1.0 - p, m)


def fee_flat(p: float, c: float) -> float:
    """对照曲线: 对名义额的平费 c·p (ROI 门槛恒定 = c)。"""
    return c * p


def roi_threshold(fee_fn, p: float) -> float:
    """打平所需的费前 alpha (ROI 口径) = 每股费 / 买入价。"""
    return fee_fn(p) / p


def wash_breakeven_price(r_new: float, m: float) -> float:
    """近确定票 carry / 刷量转负 EV 的临界价 p*。

    净 EV = (1−p)/p − r´·m; 令其为 0 ⟹ p* = 1/(1+r´·m)。
    现行驼峰 (m=0) 下净 EV = (1−p)(1/p − r) > 0 恒成立 ⟹ p* 不存在, 通道永远开着。
    """
    if m <= 0:
        return float("nan")
    return 1.0 / (1.0 + r_new * m)


def revenue_per_notional(dist, fee_fn) -> float:
    """每 $1 名义额的费收入 = E_w[g(p)/p], w 为名义额权重。"""
    return sum(w * fee_fn(p) / p for p, w in dist)


def solve_revenue_neutral(dist, fee_family, lo: float, hi: float, target: float,
                          iters: int = 200) -> float:
    """在单参数曲线族上二分求收入中性参数。fee_family(p, x) -> 每股费。"""
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if revenue_per_notional(dist, lambda p: fee_family(p, mid)) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def uniform_dist(n: int = 300) -> list[tuple[float, float]]:
    """演示用均匀分布 —— 对外发布数学部分时的默认分布, 不含任何实测常数。"""
    return [((i + 0.5) / n, 1.0 / n) for i in range(n)]


# ── ② 分布标定层 (来自我方实测的聚合量, 属派生数据) ──────────────────────

CALIBRATION = {
    # 全部来自 260803 深挖 / 260804 spec / price_expression_shadow 的实测聚合量
    "rate": 0.05,                 # per-market feeSchedule.rate 实测主流值 (572/595)
    "total_fee_usd": 5_524_178.0, # 样本期总 taker 费
    "flat_equivalent": 0.011898,  # 收入中性的名义额平费率
    # 名义额权重的三个分段 (p<0.60 / 0.60–0.90 / p≥0.90)
    "bands": ((0.001, 0.60, 0.280), (0.60, 0.90, 0.265), (0.90, 0.999, 0.455)),
}


def implied_notional() -> float:
    """总费用 / 平费率 ⟹ 隐含 taker 名义额。与报告独立给出的 $4.64 亿互为交叉验证。"""
    return CALIBRATION["total_fee_usd"] / CALIBRATION["flat_equivalent"]


def implied_mean_price() -> float:
    """由 r·E_w[1−p] = flat 解出名义额加权平均成交价 E_w[p]。两个已知数推出的硬约束。"""
    return 1.0 - CALIBRATION["flat_equivalent"] / CALIBRATION["rate"]


def _band_points(lo: float, hi: float, n: int) -> list[float]:
    return [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]


def calibrated_dist(tilts=(1.0, 1.0, 1.0), n_per_band: int = 300):
    """按分段权重构造分布; tilts 控制各档内的线性倾斜 (>0 = 档内偏向高价)。"""
    out = []
    for (lo, hi, w), t in zip(CALIBRATION["bands"], tilts):
        xs = _band_points(lo, hi, n_per_band)
        raw = [max(1.0 + t * (((x - lo) / (hi - lo)) - 0.5) * 2.0, 1e-9) for x in xs]
        s = sum(raw)
        out += [(x, w * v / s) for x, v in zip(xs, raw)]
    return out


def fit_dist(direction=(1.0, 1.0, 1.0), n_per_band: int = 300, iters: int = 200):
    """沿给定倾斜方向缩放, 使 E_w[p] 命中实测约束。返回 (dist, scale)。

    四个约束 (三个分段权重 + E_w[p]) 联合可解 ⟹ 分布不是自由参数。
    不同 `direction` 给出不同档内形状但满足同一组约束, 用于鲁棒性检验。
    """
    target = implied_mean_price()
    lo, hi = 0.0, 60.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        d = calibrated_dist(tuple(mid * x for x in direction), n_per_band)
        if sum(w * p for p, w in d) < target:
            lo = mid
        else:
            hi = mid
    s = 0.5 * (lo + hi)
    return calibrated_dist(tuple(s * x for x in direction), n_per_band), s


# ── 报表 ────────────────────────────────────────────────────────────────

SEGMENTS = ((0.0, 0.60, "p<0.60 信息交易者主场"), (0.60, 0.80, "0.60–0.80"),
            (0.80, 0.90, "0.80–0.90"), (0.90, 0.98, "0.90–0.98 carry 区"),
            (0.98, 1.0, "p≥0.98 刷量区"))


def report(m_values=(0.10, 0.15, 0.20, 0.25, 0.35, 0.50)) -> None:
    r = CALIBRATION["rate"]
    dist, _ = fit_dist()
    target = revenue_per_notional(dist, lambda p: fee_hump(p, r))

    print(f"隐含 taker 名义额  ${implied_notional()/1e6:,.1f}M   (报告独立值 $464M)")
    print(f"名义额加权均价     E_w[p] = {implied_mean_price():.4f}")
    print(f"现行有效费率       {target*100:.4f}% / $1 名义额\n")

    print("现行费表的负担分布")
    print("-" * 68)
    for lo, hi, label in SEGMENTS:
        nom = sum(w for p, w in dist if lo <= p < hi)
        fee = sum(w * fee_hump(p, r) / p for p, w in dist if lo <= p < hi)
        if nom <= 0:
            continue
        print(f"  {label:22s} 名义 {nom*100:5.1f}%   付费 {fee/target*100:5.1f}%"
              f"   该段有效费率 {fee/nom*100:.4f}%")

    print("\n替代曲线 g(p) = r´·p·max(1−p, m) 的收入中性解")
    print("-" * 68)
    print(f"{'m':>6} {'r´':>8} {'p<1−m 税变':>11} {'p≥1−m ROI 门槛':>15} {'p*':>9}"
          f" {'制造$1成交额成本':>16}")
    print(f"{'现行':>6} {r:>8.4f} {'—':>11} {'—':>15} {'不存在':>9}"
          f" {fee_hump(0.998, r)/0.998*100:>15.4f}%")
    for m in m_values:
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        print(f"{m:>6.2f} {rp:>8.4f} {(rp/r-1)*100:>10.1f}% {rp*m*100:>14.4f}%"
              f" {wash_breakeven_price(rp, m):>9.5f} {rp*m*100:>15.4f}%")

    for m in (0.20, 0.35):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        print(f"\n动态影响 (m={m}, r´={rp:.4f}) — 刷量段按留存率退出")
        print("-" * 68)
        for keep in (1.0, 0.5, 0.2, 0.0):
            rev = sum(w * (keep if p >= 0.98 else 1.0) * fee_floored(p, rp, m) / p
                      for p, w in dist)
            print(f"  刷量留存 {keep*100:5.0f}%   场馆收入 {(rev/target-1)*100:+6.1f}%")
        lost = sum(w * fee_floored(p, rp, m) / p for p, w in dist if p >= 0.98)
        mid = sum(w * fee_floored(p, rp, m) / p for p, w in dist if p < 0.80)
        need = lost / mid
        print(f"  刷量全退出时: 中间区需增量 {need*100:.1f}%, 而其单位成本降 {(1-rp/r)*100:.1f}%"
              f"  ⟹ 所需弹性 {need/(1-rp/r):.2f}")


def robustness() -> None:
    """跨四种档内形状 (全部满足同一组实测约束) 检验 r´ 的稳定性。"""
    r = CALIBRATION["rate"]
    print("鲁棒性: 不同档内形状下的收入中性 r´")
    print("-" * 68)
    for name, direction in (("全档同向", (1, 1, 1)), ("低中档为主", (1, 1, 0.001)),
                            ("中高档为主", (0.001, 1, 1)), ("高档为主", (0.2, 0.5, 1))):
        dist, _ = fit_dist(direction)
        ep = sum(w * p for p, w in dist)
        target = revenue_per_notional(dist, lambda p: fee_hump(p, r))
        row = []
        for m in (0.20, 0.35):
            rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
            row.append(f"m={m}: r´={rp:.4f}")
        hit = "OK" if abs(ep - implied_mean_price()) < 1e-3 else "约束未命中"
        print(f"  {name:12s} E_w[p]={ep:.4f} [{hit}]   " + "   ".join(row))


def selftest() -> int:
    r = CALIBRATION["rate"]
    fails = []

    # 1) 隐含名义额与报告独立值一致 (±2%)
    if abs(implied_notional() / 464e6 - 1) > 0.02:
        fails.append(f"隐含名义额 {implied_notional():.0f} 偏离 $464M 超过 2%")

    # 2) E_w[p] 恒等式
    if abs(r * (1 - implied_mean_price()) - CALIBRATION["flat_equivalent"]) > 1e-9:
        fails.append("E_w[p] 与平费率不自洽")

    # 3) 现行驼峰下刷量净 EV 恒正 (p* 不存在)
    for p in (0.90, 0.99, 0.999):
        if (1 - p) / p - fee_hump(p, r) / p <= 0:
            fails.append(f"现行费表在 p={p} 的 carry 净 EV 应恒正")

    # 4) 收入中性精度 + r´ < r + p* 落在 (0.98, 1)
    dist, _ = fit_dist()
    target = revenue_per_notional(dist, lambda p: fee_hump(p, r))
    for m in (0.10, 0.20, 0.35, 0.50):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        got = revenue_per_notional(dist, lambda p: fee_floored(p, rp, m))
        if abs(got / target - 1) > 1e-6:
            fails.append(f"m={m} 收入中性误差 {abs(got/target-1):.2e}")
        if not rp < r:
            fails.append(f"m={m} 的 r´={rp:.4f} 应严格小于 r={r}")
        ps = wash_breakeven_price(rp, m)
        if not 0.98 < ps < 1.0:
            fails.append(f"m={m} 的 p*={ps:.5f} 不在 (0.98, 1)")

    # 5) 分段分解之和 = 总收入 (守 report() 的口径: 必须按 fee/p 汇总, 不是按 fee)
    seg_sum = sum(w * fee_hump(p, r) / p for p, w in dist)
    if abs(seg_sum / target - 1) > 1e-9:
        fails.append(f"分段汇总口径错误: Σ={seg_sum:.6f} vs target={target:.6f}")
    by_seg = sum(sum(w * fee_hump(p, r) / p for p, w in dist if lo <= p < hi)
                 for lo, hi, _ in SEGMENTS)
    if abs(by_seg / target - 1) > 1e-6:
        fails.append(f"SEGMENTS 未覆盖全域: Σ段={by_seg:.6f} vs target={target:.6f}")

    # 6) 收入中性下, 刷量 100% 留存时场馆收入必须不变
    for m in (0.20, 0.35):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        rev = revenue_per_notional(dist, lambda p: fee_floored(p, rp, m))
        if abs(rev / target - 1) > 1e-6:
            fails.append(f"m={m} 量不变时收入应中性, 实得 {(rev/target-1)*100:+.2f}%")

    # 7) 曲线在 p=1−m 处连续
    for m in (0.20, 0.35):
        eps = 1e-9
        lhs, rhs = fee_floored(1 - m - eps, r, m), fee_floored(1 - m + eps, r, m)
        if abs(lhs - rhs) > 1e-9:
            fails.append(f"m={m} 在 p=1−m 处不连续")

    # 8) p<1−m 段是等比例降税 (形状不变)
    m, = (0.20,)
    rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
    ratios = [fee_floored(p, rp, m) / fee_hump(p, r) for p in (0.05, 0.2, 0.4, 0.6, 0.79)]
    if max(ratios) - min(ratios) > 1e-9:
        fails.append("p<1−m 段应为等比例降税")

    # 9) 纯代数层不依赖标定常数
    ud = uniform_dist(50)
    t = revenue_per_notional(ud, lambda p: fee_hump(p, 0.07))
    if not 0 < t < 0.07:
        fails.append("uniform 分布上的有效费率超出合理范围")

    for f in fails:
        print(f"FAIL  {f}")
    print(f"selftest: {'PASS' if not fails else str(len(fails)) + ' FAIL'} (9 组断言)")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--robustness", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if args.robustness:
        robustness()
    else:
        report()
