"""ORM 模型。所有时间戳统一存 UTC（naive）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    readings: Mapped[list["Reading"]] = relationship(back_populates="device")
    anchors: Mapped[list["Anchor"]] = relationship(back_populates="device")


class Reading(Base):
    """同一展柜同一时间戳的重复上报合并为一行。"""

    __tablename__ = "readings"
    __table_args__ = (UniqueConstraint("device_id", "ts", name="uq_reading_device_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    temperature: Mapped[float] = mapped_column(Float)
    humidity: Mapped[float] = mapped_column(Float)
    merged_count: Mapped[int] = mapped_column(Integer, default=1)  # 合并进本行的上报次数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    device: Mapped[Device] = relationship(back_populates="readings")


class Anchor(Base):
    """校准锚点：强制在该时刻（之后的第一条读数处）切分时间线。"""

    __tablename__ = "anchors"
    __table_args__ = (UniqueConstraint("device_id", "ts", name="uq_anchor_device_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    device: Mapped[Device] = relationship(back_populates="anchors")


class ReplayRun(Base):
    """每次时间线重放（分段重算）的审计记录。"""

    __tablename__ = "replay_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(32))  # ingest | anchor_add | anchor_delete
    late: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否由迟到读数触发
    changed_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None=从头
    readings_in_window: Mapped[int] = mapped_column(Integer, default=0)
    segments_produced: Mapped[int] = mapped_column(Integer, default=0)
    algo_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("replay_runs.id"))
    start_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    n: Mapped[int] = mapped_column(Integer)
    mean_temperature: Mapped[float] = mapped_column(Float)
    mean_humidity: Mapped[float] = mapped_column(Float)
    var_temperature: Mapped[float] = mapped_column(Float)
    var_humidity: Mapped[float] = mapped_column(Float)
    delta_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)  # 相对上一段
    delta_humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str] = mapped_column(String(8))  # high | low
    algo_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
