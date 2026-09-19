"""设备、读数、分段与重放审计查询。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Anchor, Device, Reading, ReplayRun, Segment
from ..schemas import (
    DeviceOut,
    ReadingsResponse,
    ReadingOut,
    ReplayRunOut,
    SegmentOut,
    SegmentsResponse,
)

router = APIRouter(prefix="/api/v1", tags=["devices"])


def _get_device_or_404(db: Session, serial: str) -> Device:
    device = db.scalar(select(Device).where(Device.serial == serial))
    if device is None:
        raise HTTPException(status_code=404, detail=f"设备不存在: {serial}")
    return device


def _segment_out(s: Segment) -> SegmentOut:
    return SegmentOut(
        start_ts=s.start_ts,
        end_ts=s.end_ts,
        n=s.n,
        mean_temperature=s.mean_temperature,
        mean_humidity=s.mean_humidity,
        delta_temperature=s.delta_temperature,
        delta_humidity=s.delta_humidity,
        confidence=s.confidence,
        algo_version=s.algo_version,
    )


@router.get("/devices", response_model=list[DeviceOut], summary="列出全部展柜")
def list_devices(db: Session = Depends(get_db)) -> list[DeviceOut]:
    out: list[DeviceOut] = []
    for d in db.scalars(select(Device).order_by(Device.serial)):
        readings = db.scalar(select(func.count(Reading.id)).where(Reading.device_id == d.id)) or 0
        segments = db.scalar(select(func.count(Segment.id)).where(Segment.device_id == d.id)) or 0
        anchors = db.scalar(select(func.count(Anchor.id)).where(Anchor.device_id == d.id)) or 0
        first_ts = db.scalar(select(func.min(Reading.ts)).where(Reading.device_id == d.id))
        last_ts = db.scalar(select(func.max(Reading.ts)).where(Reading.device_id == d.id))
        out.append(
            DeviceOut(
                serial=d.serial,
                readings=readings,
                segments=segments,
                anchors=anchors,
                first_ts=first_ts,
                last_ts=last_ts,
            )
        )
    return out


@router.get(
    "/devices/{serial}/readings",
    response_model=ReadingsResponse,
    summary="查询展柜原始读数",
)
def list_readings(
    serial: str,
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ReadingsResponse:
    device = _get_device_or_404(db, serial)
    q = select(Reading).where(Reading.device_id == device.id)
    if from_ts is not None:
        q = q.where(Reading.ts >= from_ts)
    if to_ts is not None:
        q = q.where(Reading.ts <= to_ts)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Reading.ts).limit(limit).offset(offset)).all()
    return ReadingsResponse(
        device_serial=serial,
        count=total,
        readings=[
            ReadingOut(
                ts=r.ts,
                temperature=r.temperature,
                humidity=r.humidity,
                merged_count=r.merged_count,
            )
            for r in rows
        ],
    )


@router.get(
    "/devices/{serial}/segments",
    response_model=SegmentsResponse,
    summary="查询展柜当前分段（起止、均值、变化幅度、置信标记）",
)
def list_segments(
    serial: str,
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> SegmentsResponse:
    device = _get_device_or_404(db, serial)
    q = select(Segment).where(Segment.device_id == device.id)
    if from_ts is not None:
        q = q.where(Segment.end_ts >= from_ts)
    if to_ts is not None:
        q = q.where(Segment.start_ts <= to_ts)
    rows = db.scalars(q.order_by(Segment.start_ts)).all()
    return SegmentsResponse(
        device_serial=serial,
        count=len(rows),
        segments=[_segment_out(s) for s in rows],
    )


@router.get(
    "/devices/{serial}/replays",
    response_model=list[ReplayRunOut],
    summary="查询展柜的时间线重放审计记录",
)
def list_replays(
    serial: str,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[ReplayRunOut]:
    device = _get_device_or_404(db, serial)
    rows = db.scalars(
        select(ReplayRun)
        .where(ReplayRun.device_id == device.id)
        .order_by(ReplayRun.id.desc())
        .limit(limit)
    ).all()
    return [ReplayRunOut.model_validate(r) for r in rows]
