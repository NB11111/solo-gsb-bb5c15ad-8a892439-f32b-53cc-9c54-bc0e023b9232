"""重放预览：纯内存试算，绝不写库。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Anchor, Device, Reading, Segment
from ..schemas import (
    PreviewParams,
    ReplayPreviewIn,
    ReplayPreviewResponse,
    SegmentOut,
)
from ..segmentation import Point, segment_timeline

router = APIRouter(prefix="/api/v1", tags=["replay"])


@router.post(
    "/replay-preview",
    response_model=ReplayPreviewResponse,
    summary="预览假设读数补入后的重放结果（不写库）",
)
def replay_preview(body: ReplayPreviewIn, db: Session = Depends(get_db)) -> ReplayPreviewResponse:
    device = db.scalar(select(Device).where(Device.serial == body.device_serial))

    # 库内读数 + 假设读数，按 (设备, 时间戳) 合并去重（假设读数覆盖同刻库值）
    merged: dict = {}
    anchor_ts: list = []
    current_segments: list[Segment] = []
    if device is not None:
        for r in db.scalars(
            select(Reading).where(Reading.device_id == device.id).order_by(Reading.ts)
        ):
            merged[r.ts] = (r.temperature, r.humidity)
        anchor_ts = list(db.scalars(select(Anchor.ts).where(Anchor.device_id == device.id)))
        current_segments = list(
            db.scalars(
                select(Segment)
                .where(Segment.device_id == device.id)
                .order_by(Segment.start_ts)
            )
        )
    for it in body.readings:
        merged[it.ts] = (it.temperature, it.humidity)

    penalty = body.penalty if body.penalty is not None else settings.penalty
    min_size = body.min_size if body.min_size is not None else settings.min_size

    points = [Point(ts=ts, temperature=t, humidity=h) for ts, (t, h) in merged.items()]
    computed = segment_timeline(
        points,
        anchor_ts,
        penalty=penalty,
        min_size=min_size,
        min_var=settings.min_var,
        min_confident_len=settings.min_confident_len,
        confidence_t=settings.confidence_t,
    )

    preview = [
        SegmentOut(
            start_ts=c.start_ts,
            end_ts=c.end_ts,
            n=c.n,
            mean_temperature=c.mean_temperature,
            mean_humidity=c.mean_humidity,
            delta_temperature=c.delta_temperature,
            delta_humidity=c.delta_humidity,
            confidence=c.confidence,
            algo_version=f"preview(penalty={penalty:g},min_size={min_size})",
        )
        for c in computed
    ]

    current_bounds = [(s.start_ts, s.end_ts, s.n) for s in current_segments]
    preview_bounds = [(c.start_ts, c.end_ts, c.n) for c in computed]

    db.rollback()  # 显式收尾：本端点只读，绝不落库
    return ReplayPreviewResponse(
        device_serial=body.device_serial,
        persisted=False,
        params=PreviewParams(penalty=penalty, min_size=min_size),
        current_count=len(current_segments),
        preview_count=len(preview),
        would_change=current_bounds != preview_bounds,
        preview=preview,
    )
