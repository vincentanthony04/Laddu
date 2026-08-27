"""Canonical Asia/Kolkata reporting windows for Model Paper economics."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from core.india_time import INDIA_TZ


class ModelPaperPerformancePeriodAuthority:
    authority = "ModelPaperPerformancePeriodAuthority"
    authority_version = "1.1.0-causal-asof"

    @classmethod
    def normalize(cls, at: datetime) -> datetime:
        if at.tzinfo is None:
            at = at.replace(tzinfo=INDIA_TZ)
        return at.astimezone(INDIA_TZ)

    @classmethod
    def start(cls, label: str, at: datetime) -> Optional[datetime]:
        now = cls.normalize(at)
        label = str(label or "").lower()
        if label == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if label == "week":
            return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        if label == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if label == "quarter":
            quarter_month = ((now.month - 1) // 3) * 3 + 1
            return now.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        if label == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        if label == "all":
            return None
        raise ValueError(f"unsupported performance period: {label}")

    @classmethod
    def contains(cls, closed_at: datetime | None, label: str, at: datetime) -> bool:
        if closed_at is None:
            return False
        closed = cls.normalize(closed_at)
        now = cls.normalize(at)
        start = cls.start(label, now)
        if closed > now:
            return False
        return start is None or closed >= start


DEFAULT_MODEL_PAPER_PERFORMANCE_PERIOD_AUTHORITY = ModelPaperPerformancePeriodAuthority()
