"""分段服务：锚点切分、置信标记、受影响时间线重放（事务内替换分段）。"""
from __future__ import annotations

import math
import threading
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .changepoint import pelt_segment
from .config import Settings
from .models import Anchor, Reading, ReplayRun, Segment

ALGO_NAME = "pelt-mvnormal-v1"

# 写路径（上报 / 锚点变更 → 重放）串行化，保证 SQLite 下分段替换的一致性
MUTATION_LOCK = threading.RLock()

_EPS = 1e-9


def algo_version(s: Settings) -> str:
    return f"{ALGO_NAME}(penalty={s.penalty:g},min_size={s.min_size},min_var={s.min_var:g})"


@dataclass(frozen=True)
class Point:
    ts: datetime
    temperature: float
    humidity: float


@dataclass(frozen=True)
class ComputedSegment:
    start_ts: datetime
    end_ts: datetime
    n: int
    mean_temperature: float
    mean_humidity: float
    var_temperature: float
    var_humidity: float
    delta_temperature: float | None
    delta_humidity: float | None
    confidence: str  # "high" | "low"


def window_start_for(anchor_ts: Sequence[datetime], changed_from: datetime) -> datetime | None:
    """受影响时间线起点：changed_from 之前（含）最近的锚点；无则 None（从头重放）。

    锚点是强制断点，断点两侧的分段相互独立，因此断点之前的分段不受新数据影响。
    """
    prior = [a for a in anchor_ts if a <= changed_from]
    return max(prior) if prior else None


def segment_timeline(
    points: Sequence[Point],
    anchor_ts: Sequence[datetime],
    *,
    penalty: float,
    min_size: int,
    min_var: float,
    min_confident_len: int,
    confidence_t: float,
) -> list[ComputedSegment]:
    """纯函数：对（可含锚点的）时间线做确定性分段，不写任何状态。"""
    pts = sorted(points, key=lambda p: p.ts)
    n = len(pts)
    if n == 0:
        return []
    ts_list = [p.ts for p in pts]

    # 锚点 → 强制切分位置：第一条 ts >= anchor 的读数之前切开
    cuts: set[int] = set()
    for a in sorted(anchor_ts):
        i = bisect_left(ts_list, a)
        if 0 < i < n:
            cuts.add(i)
    bounds = [0] + sorted(cuts) + [n]

    temps = [p.temperature for p in pts]
    hums = [p.humidity for p in pts]

    spans: list[tuple[int, int]] = []
    for c0, c1 in zip(bounds, bounds[1:]):
        for a, b in pelt_segment(
            temps[c0:c1], hums[c0:c1], penalty=penalty, min_size=min_size, min_var=min_var
        ):
            spans.append((c0 + a, c0 + b))

    out: list[ComputedSegment] = []
    prev: ComputedSegment | None = None
    for a, b in spans:
        m = b - a
        st = temps[a:b]
        sh = hums[a:b]
        mean_t = sum(st) / m
        mean_h = sum(sh) / m
        var_t = sum((x - mean_t) ** 2 for x in st) / m
        var_h = sum((x - mean_h) ** 2 for x in sh) / m

        if prev is None:
            d_t = d_h = None
            confidence = "high" if m >= min_confident_len else "low"
        else:
            d_t = mean_t - prev.mean_temperature
            d_h = mean_h - prev.mean_humidity
            # 方差同样取下限：恒定段上的微小跳变不应判为高置信
            pv_t = max(prev.var_temperature, min_var)
            pv_h = max(prev.var_humidity, min_var)
            cv_t = max(var_t, min_var)
            cv_h = max(var_h, min_var)
            se_t = math.sqrt(pv_t / prev.n + cv_t / m + _EPS)
            se_h = math.sqrt(pv_h / prev.n + cv_h / m + _EPS)
            t_stat = max(abs(d_t) / se_t, abs(d_h) / se_h)
            confidence = "high" if (m >= min_confident_len and t_stat >= confidence_t) else "low"

        seg = ComputedSegment(
            start_ts=ts_list[a],
            end_ts=ts_list[b - 1],
            n=m,
            mean_temperature=mean_t,
            mean_humidity=mean_h,
            var_temperature=var_t,
            var_humidity=var_h,
            delta_temperature=d_t,
            delta_humidity=d_h,
            confidence=confidence,
        )
        out.append(seg)
        prev = seg
    return out


def recompute_device(
    db: Session,
    device_id: int,
    *,
    trigger: str,
    late: bool,
    changed_from: datetime | None,
    window_start: datetime | None,
    settings: Settings,
) -> ReplayRun:
    """重放受影响时间线：删除窗口内旧分段，重算并写入新分段（调用方负责提交事务）。"""
    q = (
        select(Reading)
        .where(Reading.device_id == device_id)
        .order_by(Reading.ts, Reading.id)
    )
    if window_start is not None:
        q = q.where(Reading.ts >= window_start)
    readings = list(db.scalars(q))

    anchor_q = select(Anchor.ts).where(Anchor.device_id == device_id).order_by(Anchor.ts)
    anchors = [a for a in db.scalars(anchor_q) if window_start is None or a >= window_start]

    points = [Point(ts=r.ts, temperature=r.temperature, humidity=r.humidity) for r in readings]
    computed = segment_timeline(
        points,
        anchors,
        penalty=settings.penalty,
        min_size=settings.min_size,
        min_var=settings.min_var,
        min_confident_len=settings.min_confident_len,
        confidence_t=settings.confidence_t,
    )

    del_q = delete(Segment).where(Segment.device_id == device_id)
    if window_start is not None:
        del_q = del_q.where(Segment.start_ts >= window_start)
    db.execute(del_q)

    version = algo_version(settings)
    run = ReplayRun(
        device_id=device_id,
        trigger=trigger,
        late=late,
        changed_from=changed_from,
        window_start=window_start,
        readings_in_window=len(readings),
        segments_produced=len(computed),
        algo_version=version,
    )
    db.add(run)
    db.flush()

    for cs in computed:
        db.add(
            Segment(
                device_id=device_id,
                run_id=run.id,
                start_ts=cs.start_ts,
                end_ts=cs.end_ts,
                n=cs.n,
                mean_temperature=cs.mean_temperature,
                mean_humidity=cs.mean_humidity,
                var_temperature=cs.var_temperature,
                var_humidity=cs.var_humidity,
                delta_temperature=cs.delta_temperature,
                delta_humidity=cs.delta_humidity,
                confidence=cs.confidence,
                algo_version=version,
            )
        )
    db.flush()
    return run
