"""运行配置：全部可通过 APP_* 环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # SQLite 连接串；容器内默认挂到 /data 卷
    database_url: str = os.getenv("APP_DATABASE_URL", "sqlite:///./cpc.db")
    # PELT 惩罚项 beta：越大分段越少（越保守）
    penalty: float = float(os.getenv("APP_PENALTY", "12.0"))
    # 每段最少读数条数
    min_size: int = int(os.getenv("APP_MIN_SIZE", "3"))
    # 方差下限（传感器分辨率）：防止零方差恒定段使代价无界
    min_var: float = float(os.getenv("APP_MIN_VAR", "0.01"))
    # 段长度达到该值才可能标记为高置信
    min_confident_len: int = int(os.getenv("APP_MIN_CONFIDENT_LEN", "6"))
    # 相邻段均值跳变的 t 统计量阈值（置信判定用）
    confidence_t: float = float(os.getenv("APP_CONFIDENCE_T", "2.0"))


settings = Settings()
