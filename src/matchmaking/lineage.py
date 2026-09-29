"""谱系事件：记录从最初意向到签约或终止的完整转换链。

每次状态变化都追加一条不可变事件，使用者沿 ``intent_id`` /
``candidate_id`` / ``project_id`` 任一入口都能回放全过程。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LineageEvent:
    seq: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    intent_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "detail": self.detail,
            "intent_ids": list(self.intent_ids),
            "candidate_ids": list(self.candidate_ids),
            "project_id": self.project_id,
        }
