"""确定性 PELT 变点检测（带惩罚项的最优分段 + 剪枝）。

代价函数：段内温湿度二维高斯对数似然（对角方差），
    cost(a, b) = n * (log(max(var_t, min_var) + eps) + log(max(var_h, min_var) + eps))
目标：min  Σ cost(段) + beta * 段数

方差下限 min_var 相当于传感器分辨率：避免零方差恒定段让代价趋于 -inf，
使惩罚项 beta 有确定、可解释的量纲（一次切分需换来的最小对数似然收益）。

确定性保证：浮点求和顺序固定；并列时取索引较小者（严格小于比较）。
"""
from __future__ import annotations

import math

_EPS = 1e-9


def _prefix_sums(xs: list[float]) -> tuple[list[float], list[float]]:
    s1, s2 = [0.0], [0.0]
    for x in xs:
        s1.append(s1[-1] + x)
        s2.append(s2[-1] + x * x)
    return s1, s2


def pelt_segment(
    temps: list[float],
    hums: list[float],
    *,
    penalty: float,
    min_size: int,
    min_var: float = 0.01,
) -> list[tuple[int, int]]:
    """对单条连续时间线做分段，返回 [start, end) 索引区间列表（覆盖全部输入）。"""
    n = len(temps)
    if n == 0:
        return []
    min_size = max(1, int(min_size))
    t1, t2 = _prefix_sums(temps)
    h1, h2 = _prefix_sums(hums)

    def cost(a: int, b: int) -> float:  # 段 [a, b)
        m = b - a
        vt = (t2[b] - t2[a]) / m - ((t1[b] - t1[a]) / m) ** 2
        vh = (h2[b] - h2[a]) / m - ((h1[b] - h1[a]) / m) ** 2
        if vt < min_var:
            vt = min_var
        if vh < min_var:
            vh = min_var
        return m * (math.log(vt + _EPS) + math.log(vh + _EPS))

    # F[t] = 前 t 个点的最优目标值；cp[t] = 对应的分段起点集合（不含 0）
    F = [0.0] * (n + 1)
    F[0] = -penalty
    cp: list[tuple[int, ...] | None] = [None] * (n + 1)
    cp[0] = ()
    candidates = [0]
    for t in range(1, n + 1):
        best_val = math.inf
        best_s = 0
        for s in candidates:
            if t - s < min_size:
                continue
            v = F[s] + cost(s, t) + penalty
            if v < best_val:  # 严格小于：并列保留先出现（更小）的 s
                best_val = v
                best_s = s
        if best_val is math.inf:
            # t < min_size：只能整段，无切分
            F[t] = cost(0, t)
            cp[t] = ()
        else:
            F[t] = best_val
            cp[t] = (cp[best_s] + (best_s,)) if best_s > 0 else ()
        # PELT 剪枝（K=0）
        candidates = [s for s in candidates if F[s] + cost(s, t) <= F[t]]
        # 切点必须满足首段长度 >= min_size，故小于 min_size 的位置不入候选
        if t >= min_size:
            candidates.append(t)

    starts = [s for s in (cp[n] or ()) if s > 0]
    bounds = [0] + sorted(starts) + [n]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
