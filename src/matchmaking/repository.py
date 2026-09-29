"""仓储：内存集合，可选 JSON 文件持久化。

撮合评估与事件写入通过 ``lock`` 串行化，避免同一批资源被超卖。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import (
    Organization, Contact, Capability, Resource, Allocation, Exclusion,
    Demand, DemandVersion, Intent, Candidate, RejectedMatch, MatchRun, Project,
    TimelineEvent,
)


class Repository:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.organizations: dict[str, Organization] = {}
        self.contacts: dict[str, Contact] = {}
        self.capabilities: dict[str, Capability] = {}
        self.resources: dict[str, Resource] = {}
        self.allocations: dict[str, Allocation] = {}
        self.exclusions: dict[str, Exclusion] = {}
        self.intents: dict[str, Intent] = {}
        self.demands: dict[str, Demand] = {}
        self.demand_versions: dict[str, DemandVersion] = {}
        self.candidates: dict[str, Candidate] = {}
        self.rejected_matches: dict[str, RejectedMatch] = {}
        self.runs: dict[str, MatchRun] = {}
        self.projects: dict[str, Project] = {}
        self.timeline: list[TimelineEvent] = []
        if self.path and self.path.exists():
            self.load()

    # ---- 持久化 ----------------------------------------------------------

    _COLLECTIONS = [
        ("organizations", Organization), ("contacts", Contact),
        ("capabilities", Capability), ("resources", Resource),
        ("allocations", Allocation), ("exclusions", Exclusion),
        ("intents", Intent), ("demands", Demand),
        ("demand_versions", DemandVersion), ("candidates", Candidate),
        ("rejected_matches", RejectedMatch), ("runs", MatchRun),
        ("projects", Project), ("timeline", TimelineEvent),
    ]

    def save(self) -> None:
        if not self.path:
            return
        payload: dict[str, Any] = {}
        for name, _ in self._COLLECTIONS:
            value = getattr(self, name)
            if isinstance(value, dict):
                payload[name] = [item.to_dict() for item in value.values()]
            else:
                payload[name] = [item.to_dict() for item in value]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def load(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for name, cls in self._COLLECTIONS:
            items = [cls.from_dict(d) for d in payload.get(name, [])]
            if name == "timeline":
                self.timeline = items
            else:
                getattr(self, name).clear()
                for item in items:
                    getattr(self, name)[item.id] = item
