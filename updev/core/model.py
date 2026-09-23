"""Core data model.

Every backend speaks exactly one language: it returns a list of `Device`.
Rendering, filtering, export and the dashboard only ever touch this model —
nobody downstream needs to know whether a device came from sysfs, an ioctl,
an ARP table or a subprocess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """What sort of thing a device is. Drives grouping and icon choice."""

    HOST = "host"
    SOC = "soc"
    POWER = "power"
    STORAGE = "storage"
    USB = "usb"
    I2C = "i2c"
    SPI = "spi"
    NFC = "nfc"
    SERIAL = "serial"
    CAMERA = "camera"
    GPIO = "gpio"
    NET_IFACE = "net-iface"
    NET_HOST = "net-host"
    BLUETOOTH = "bluetooth"
    DISPLAY = "display"
    THERMAL = "thermal"
    UNKNOWN = "unknown"


class Status(StrEnum):
    """Health of a device at scan time."""

    ONLINE = "online"      # present and working
    IDLE = "idle"          # present but nothing on it (empty bus, unplugged port)
    DEGRADED = "degraded"  # working, but something is wrong
    DISABLED = "disabled"  # hardware exists, switched off in config
    ABSENT = "absent"      # expected here, not found
    ERROR = "error"        # probe blew up
    UNKNOWN = "unknown"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# Sort weight so the ugliest things float to the top of a table.
_STATUS_WEIGHT = {
    Status.ERROR: 0,
    Status.DEGRADED: 1,
    Status.ABSENT: 2,
    Status.DISABLED: 3,
    Status.UNKNOWN: 4,
    Status.IDLE: 5,
    Status.ONLINE: 6,
}

_SEVERITY_WEIGHT = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}


def status_weight(s: Status) -> int:
    return _STATUS_WEIGHT.get(s, 9)


def severity_weight(s: Severity) -> int:
    return _SEVERITY_WEIGHT.get(s, 9)


@dataclass(slots=True)
class Issue:
    """Something worth telling the user about, ideally with a way to fix it."""

    severity: Severity
    message: str
    fix: str = ""          # shell command or config line that resolves it
    doc: str = ""          # short explanation of *why*

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "message": self.message,
            "fix": self.fix,
            "doc": self.doc,
        }


@dataclass(slots=True)
class Action:
    """An operation this device supports, expressed as an updev command."""

    name: str
    description: str
    command: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "command": self.command}


@dataclass(slots=True)
class Device:
    """One addressable thing: a bus, a chip, a NIC, a neighbour on the LAN."""

    uid: str                       # stable across scans, e.g. "usb:3-2", "i2c:1:0x3c"
    kind: Kind
    name: str
    status: Status = Status.UNKNOWN
    summary: str = ""              # one line, shown in the main table
    bus: str = ""                  # "usb", "i2c-1", "eth0"
    address: str = ""              # "3-2", "0x3c", "172.30.1.5"
    vendor: str = ""
    model: str = ""
    serial: str = ""
    driver: str = ""
    node: str = ""                 # /dev path if it has one
    parent: str | None = None      # uid of parent device, for tree view
    detail: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)   # numeric, graphable
    tags: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    # -- convenience -------------------------------------------------------

    @property
    def label(self) -> str:
        """Best human name we can build. Only prepends the vendor when it isn't
        already in the name — "Raspberry Pi Raspberry Pi 5" helps nobody."""
        base = self.name or self.model or self.uid
        if self.vendor and self.vendor.lower() not in base.lower():
            return f"{self.vendor} {base}"
        return base

    @property
    def worst(self) -> Severity | None:
        if not self.issues:
            return None
        return min((i.severity for i in self.issues), key=severity_weight)

    def issue(self, severity: Severity, message: str, fix: str = "", doc: str = "") -> None:
        self.issues.append(Issue(severity, message, fix, doc))

    def act(self, name: str, description: str, command: str) -> None:
        self.actions.append(Action(name, description, command))

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "kind": str(self.kind),
            "name": self.name,
            "label": self.label,
            "status": str(self.status),
            "summary": self.summary,
            "bus": self.bus,
            "address": self.address,
            "vendor": self.vendor,
            "model": self.model,
            "serial": self.serial,
            "driver": self.driver,
            "node": self.node,
            "parent": self.parent,
            "detail": self.detail,
            "metrics": self.metrics,
            "tags": self.tags,
            "issues": [i.as_dict() for i in self.issues],
            "actions": [a.as_dict() for a in self.actions],
            "ts": self.ts,
        }


@dataclass(slots=True)
class BackendReport:
    """Per-backend bookkeeping: did it run, how long, did it explode."""

    name: str
    available: bool
    reason: str = ""           # why unavailable, if it is
    ok: bool = True
    error: str = ""
    duration: float = 0.0
    count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "ok": self.ok,
            "error": self.error,
            "duration": round(self.duration, 4),
            "count": self.count,
        }


@dataclass(slots=True)
class ScanResult:
    devices: list[Device] = field(default_factory=list)
    reports: list[BackendReport] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0

    @property
    def duration(self) -> float:
        return self.finished - self.started

    def by_kind(self) -> dict[Kind, list[Device]]:
        out: dict[Kind, list[Device]] = {}
        for d in self.devices:
            out.setdefault(d.kind, []).append(d)
        return out

    def by_uid(self) -> dict[str, Device]:
        return {d.uid: d for d in self.devices}

    def issues(self) -> list[tuple[Device, Issue]]:
        pairs = [(d, i) for d in self.devices for i in d.issues]
        pairs.sort(key=lambda p: (severity_weight(p[1].severity), p[0].uid))
        return pairs

    def find(self, query: str) -> list[Device]:
        """Loose match on uid, name, address, node — whatever the user typed."""
        q = query.strip().lower()
        exact = [d for d in self.devices if d.uid.lower() == q]
        if exact:
            return exact
        return [
            d
            for d in self.devices
            if q in d.uid.lower()
            or q in d.name.lower()
            or q in d.label.lower()
            or q in d.address.lower()
            or q in d.node.lower()
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "finished": self.finished,
            "duration": round(self.duration, 4),
            "devices": [d.as_dict() for d in self.devices],
            "backends": [r.as_dict() for r in self.reports],
        }
