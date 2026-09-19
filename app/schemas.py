"""请求/响应模式。所有时间戳均为 UTC（ISO 8601）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_utc(dt: datetime) -> datetime:
    """统一为 UTC naive 存储；无 tzinfo 的输入按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class ReadingPoint(BaseModel):
    ts: datetime = Field(description="采样时间（ISO 8601，建议带时区；缺省按 UTC）")
    temperature: float = Field(ge=-80.0, le=120.0, description="温度 ℃")
    humidity: float = Field(ge=0.0, le=100.0, description="相对湿度 %RH")

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)


class ReadingIn(ReadingPoint):
    device_serial: str = Field(min_length=1, max_length=64, description="设备序号（展柜编号）")


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(min_length=1, max_length=5000)


class ReplaySummary(BaseModel):
    run_id: int
    trigger: str
    late: bool
    changed_from: datetime | None
    window_start: datetime | None
    readings_in_window: int
    segments_produced: int
    algo_version: str


class DeviceIngestSummary(BaseModel):
    device_serial: str
    received: int = Field(description="本批次该设备上报条数")
    inserted: int = Field(description="新增读数条数")
    merged: int = Field(description="与既有读数冲突并合并（覆盖数值）的条数")
    unchanged: int = Field(description="重复上报且数值一致、无需变更的条数")
    duplicates_in_batch: int = Field(description="本批次内部同时间戳去重条数")
    late: bool = Field(description="是否包含迟到读数（改写了已存在的读数）")
    replay: ReplaySummary | None = Field(description="触发的时间线重放；无变更则为 null")


class IngestResponse(BaseModel):
    devices: list[DeviceIngestSummary]


class SegmentOut(BaseModel):
    start_ts: datetime
    end_ts: datetime
    n: int = Field(description="段内读数条数")
    mean_temperature: float
    mean_humidity: float
    delta_temperature: float | None = Field(description="相对上一段的均值变化（首段为 null）")
    delta_humidity: float | None
    confidence: Literal["high", "low"]
    algo_version: str


class SegmentsResponse(BaseModel):
    device_serial: str
    count: int
    segments: list[SegmentOut]


class AnchorIn(BaseModel):
    ts: datetime
    note: str = Field(default="", max_length=255)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)


class AnchorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    note: str
    created_at: datetime


class AnchorResponse(BaseModel):
    device_serial: str
    anchor: AnchorOut
    replay: ReplaySummary | None


class ReadingOut(BaseModel):
    ts: datetime
    temperature: float
    humidity: float
    merged_count: int


class ReadingsResponse(BaseModel):
    device_serial: str
    count: int
    readings: list[ReadingOut]


class DeviceOut(BaseModel):
    serial: str
    readings: int
    segments: int
    anchors: int
    first_ts: datetime | None
    last_ts: datetime | None


class ReplayRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    late: bool
    changed_from: datetime | None
    window_start: datetime | None
    readings_in_window: int
    segments_produced: int
    algo_version: str
    created_at: datetime


class ReplayPreviewIn(BaseModel):
    device_serial: str = Field(min_length=1, max_length=64)
    readings: list[ReadingPoint] = Field(default_factory=list, max_length=5000,
                                         description="假设补入的读数（与库内数据按时间戳合并去重）")
    penalty: float | None = Field(default=None, gt=0, description="覆盖默认惩罚项做 what-if")
    min_size: int | None = Field(default=None, ge=1, description="覆盖默认最小段长")


class PreviewParams(BaseModel):
    penalty: float
    min_size: int


class ReplayPreviewResponse(BaseModel):
    device_serial: str
    persisted: Literal[False] = Field(description="恒为 false：预览绝不写库")
    params: PreviewParams
    current_count: int
    preview_count: int
    would_change: bool = Field(description="若真实补入，分段结果是否会变化")
    preview: list[SegmentOut]
