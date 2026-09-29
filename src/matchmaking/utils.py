"""通用工具：标识与时间。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 可替换的时钟，便于测试确定性时间线。
Clock = Callable[[], str]
default_clock: Clock = now_iso
