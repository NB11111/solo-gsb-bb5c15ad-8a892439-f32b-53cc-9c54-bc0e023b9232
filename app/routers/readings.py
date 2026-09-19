"""读数批量上报：去重合并 + 迟到读数触发时间线重放。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Anchor, Device, Reading
from ..schemas import (
    DeviceIngestSummary,
    IngestResponse,
    ReadingBatchIn,
    ReplaySummary,
)
from ..segmentation import MUTATION_LOCK, recompute_device, window_start_for

router = APIRouter(prefix="/api/v1", tags=["readings"])


def _get_or_create_device(db: Session, serial: str) -> Device:
    device = db.scalar(select(Device).where(Device.serial == serial))
    if device is None:
        device = Device(serial=serial)
        db.add(device)
        db.flush()
    return device


def _replay_summary(run) -> ReplaySummary:
    return ReplaySummary(
        run_id=run.id,
        trigger=run.trigger,
        late=run.late,
        changed_from=run.changed_from,
        window_start=run.window_start,
        readings_in_window=run.readings_in_window,
        segments_produced=run.segments_produced,
        algo_version=run.algo_version,
    )


@router.post(
    "/readings",
    response_model=IngestResponse,
    summary="批量上报温湿度读数（同设备同时间戳自动合并；迟到读数触发受影响时间线重放）",
)
def ingest_readings(batch: ReadingBatchIn, db: Session = Depends(get_db)) -> IngestResponse:
    by_serial: dict[str, list] = {}
    for item in batch.readings:
        by_serial.setdefault(item.device_serial, []).append(item)

    summaries: list[DeviceIngestSummary] = []
    with MUTATION_LOCK:
        for serial, items in by_serial.items():
            device = _get_or_create_device(db, serial)

            # 批次内去重：同时间戳后到的覆盖先到的
            deduped = {it.ts: it for it in items}
            ts_keys = list(deduped.keys())

            existing = {
                r.ts: r
                for r in db.scalars(
                    select(Reading).where(
                        Reading.device_id == device.id, Reading.ts.in_(ts_keys)
                    )
                )
            }
            max_ts_before = db.scalar(
                select(func.max(Reading.ts)).where(Reading.device_id == device.id)
            )

            inserted = merged = unchanged = 0
            changed_ts: list = []
            for ts, it in deduped.items():
                row = existing.get(ts)
                if row is None:
                    db.add(
                        Reading(
                            device_id=device.id,
                            ts=ts,
                            temperature=it.temperature,
                            humidity=it.humidity,
                            merged_count=1,
                        )
                    )
                    inserted += 1
                    changed_ts.append(ts)
                elif row.temperature == it.temperature and row.humidity == it.humidity:
                    unchanged += 1  # 幂等重报：数值一致，不触发重放
                else:
                    row.temperature = it.temperature
                    row.humidity = it.humidity
                    row.merged_count += 1
                    merged += 1
                    changed_ts.append(ts)

            run = None
            late = False
            if changed_ts:
                db.flush()  # 重放前先把本批读数落进会话（autoflush 关闭）
                changed_from = min(changed_ts)
                late = max_ts_before is not None and changed_from <= max_ts_before
                anchor_ts = list(
                    db.scalars(select(Anchor.ts).where(Anchor.device_id == device.id))
                )
                window_start = window_start_for(anchor_ts, changed_from)
                run = recompute_device(
                    db,
                    device.id,
                    trigger="ingest",
                    late=late,
                    changed_from=changed_from,
                    window_start=window_start,
                    settings=settings,
                )

            summaries.append(
                DeviceIngestSummary(
                    device_serial=serial,
                    received=len(items),
                    inserted=inserted,
                    merged=merged,
                    unchanged=unchanged,
                    duplicates_in_batch=len(items) - len(deduped),
                    late=late,
                    replay=_replay_summary(run) if run else None,
                )
            )
        db.commit()
    return IngestResponse(devices=summaries)
