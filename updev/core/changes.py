"""Diffing consecutive scans.

Shared by the dashboard's activity panel, `updev activity` and the USB zone —
all three are asking the same question ("what moved?"), so they ask it once,
here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from .model import Device, Status


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    STATUS = "status"


@dataclass(slots=True)
class Change:
    kind: ChangeKind
    device: Device
    previous_status: Status | None = None
    when: float = field(default_factory=time.time)

    @property
    def uid(self) -> str:
        return self.device.uid

    def as_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "uid": self.device.uid,
            "label": self.device.label,
            "kind_of_device": str(self.device.kind),
            "status": str(self.device.status),
            "previous_status": str(self.previous_status) if self.previous_status else None,
            "summary": self.device.summary,
            "when": self.when,
        }


def diff_devices(
    previous: dict[str, Device],
    current: dict[str, Device],
) -> list[Change]:
    """What changed between two scans.

    An empty `previous` yields nothing — the first scan is a baseline, not a
    burst of "everything appeared".
    """
    if not previous:
        return []

    changes: list[Change] = []
    for uid, dev in current.items():
        old = previous.get(uid)
        if old is None:
            changes.append(Change(ChangeKind.ADDED, dev))
        elif old.status != dev.status:
            changes.append(Change(ChangeKind.STATUS, dev, previous_status=old.status))
    for uid, old in previous.items():
        if uid not in current:
            changes.append(Change(ChangeKind.REMOVED, old))
    return changes
