"""锚点校准：强制分段边界；增删锚点会重放受影响时间线。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Anchor, Device
from ..schemas import AnchorIn, AnchorOut, AnchorResponse, ReplaySummary
from ..segmentation import MUTATION_LOCK, recompute_device, window_start_for
from .readings import _get_or_create_device, _replay_summary

router = APIRouter(prefix="/api/v1", tags=["anchors"])


def _get_device_or_404(db: Session, serial: str) -> Device:
    device = db.scalar(select(Device).where(Device.serial == serial))
    if device is None:
        raise HTTPException(status_code=404, detail=f"设备不存在: {serial}")
    return device


@router.get(
    "/devices/{serial}/anchors",
    response_model=list[AnchorOut],
    summary="列出展柜的校准锚点",
)
def list_anchors(serial: str, db: Session = Depends(get_db)) -> list[AnchorOut]:
    device = _get_device_or_404(db, serial)
    rows = db.scalars(
        select(Anchor).where(Anchor.device_id == device.id).order_by(Anchor.ts)
    ).all()
    return [AnchorOut.model_validate(a) for a in rows]


@router.post(
    "/devices/{serial}/anchors",
    response_model=AnchorResponse,
    status_code=201,
    summary="新增校准锚点（同时间戳重复提交仅更新备注，不触发重放）",
)
def add_anchor(serial: str, body: AnchorIn, db: Session = Depends(get_db)) -> AnchorResponse:
    with MUTATION_LOCK:
        device = _get_or_create_device(db, serial)
        existing = db.scalar(
            select(Anchor).where(Anchor.device_id == device.id, Anchor.ts == body.ts)
        )
        if existing is not None:
            existing.note = body.note  # 幂等：分段不受影响
            db.commit()
            return AnchorResponse(
                device_serial=serial, anchor=AnchorOut.model_validate(existing), replay=None
            )

        # 窗口必须在加锚点之前计算：旧分段只被既有锚点约束
        old_anchor_ts = list(
            db.scalars(select(Anchor.ts).where(Anchor.device_id == device.id))
        )
        window_start = window_start_for(old_anchor_ts, body.ts)

        anchor = Anchor(device_id=device.id, ts=body.ts, note=body.note)
        db.add(anchor)
        db.flush()
        run = recompute_device(
            db,
            device.id,
            trigger="anchor_add",
            late=False,
            changed_from=body.ts,
            window_start=window_start,
            settings=settings,
        )
        db.commit()
        return AnchorResponse(
            device_serial=serial,
            anchor=AnchorOut.model_validate(anchor),
            replay=ReplaySummary(
                run_id=run.id,
                trigger=run.trigger,
                late=run.late,
                changed_from=run.changed_from,
                window_start=run.window_start,
                readings_in_window=run.readings_in_window,
                segments_produced=run.segments_produced,
                algo_version=run.algo_version,
            ),
        )


@router.delete(
    "/devices/{serial}/anchors/{anchor_id}",
    response_model=AnchorResponse,
    summary="删除校准锚点并重放受影响时间线",
)
def delete_anchor(serial: str, anchor_id: int, db: Session = Depends(get_db)) -> AnchorResponse:
    with MUTATION_LOCK:
        device = _get_device_or_404(db, serial)
        anchor = db.scalar(
            select(Anchor).where(Anchor.device_id == device.id, Anchor.id == anchor_id)
        )
        if anchor is None:
            raise HTTPException(status_code=404, detail=f"锚点不存在: {anchor_id}")
        anchor_out = AnchorOut.model_validate(anchor)
        removed_ts = anchor.ts
        db.delete(anchor)
        db.flush()

        remaining_ts = list(
            db.scalars(select(Anchor.ts).where(Anchor.device_id == device.id))
        )
        window_start = window_start_for(remaining_ts, removed_ts)
        run = recompute_device(
            db,
            device.id,
            trigger="anchor_delete",
            late=False,
            changed_from=removed_ts,
            window_start=window_start,
            settings=settings,
        )
        db.commit()
        return AnchorResponse(
            device_serial=serial, anchor=anchor_out, replay=_replay_summary(run)
        )
