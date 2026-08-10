"""Backend contract and the scan orchestrator.

Backends are deliberately dumb and synchronous — probing hardware means
blocking ioctls and subprocesses, and pretending otherwise buys nothing.
The orchestrator runs them concurrently in a thread pool with a hard
per-backend timeout, so one wedged bus can't hang the whole scan.
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .model import BackendReport, Device, Kind, ScanResult, status_weight


@dataclass(slots=True)
class ProbeContext:
    """Knobs a backend may consult. Defaults are the safe, fast, read-only path."""

    deep: bool = False              # allow slower probes (bus address scans, LAN sweep)
    timeout: float = 20.0           # per-backend budget in seconds
    lan_cidr: str = ""              # override the subnet to sweep
    lan_ports: tuple[int, ...] = (22, 80, 443, 445, 548, 3389, 5000, 8080, 9100)
    lan_concurrency: int = 256
    resolve_names: bool = True      # reverse-DNS LAN neighbours
    include: frozenset[str] = frozenset()   # backend names to keep ("" = all)
    exclude: frozenset[str] = frozenset()


class Backend:
    """Base class. Subclass, set `name`/`kinds`, implement `available` + `probe`."""

    name: str = "backend"
    title: str = "Backend"
    kinds: tuple[Kind, ...] = ()
    #: Backends flagged slow only run with --deep (or when named explicitly).
    slow: bool = False

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        """(usable?, why-not). Cheap checks only — no probing here."""
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        raise NotImplementedError

    # Backends may expose extra verbs; the CLI wires these up by name.
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Backend {self.name}>"


@dataclass
class Scanner:
    """Holds the backend set and runs scans against it."""

    backends: list[Backend] = field(default_factory=list)

    def register(self, backend: Backend) -> Backend:
        self.backends.append(backend)
        return backend

    def get(self, name: str) -> Backend | None:
        return next((b for b in self.backends if b.name == name), None)

    def selected(self, ctx: ProbeContext) -> list[Backend]:
        picked = []
        for b in self.backends:
            if ctx.include and b.name not in ctx.include:
                continue
            if b.name in ctx.exclude:
                continue
            # Slow backends stay out of a default scan unless asked for by name.
            if b.slow and not ctx.deep and b.name not in ctx.include:
                continue
            picked.append(b)
        return picked

    def scan(self, ctx: ProbeContext | None = None) -> ScanResult:
        ctx = ctx or ProbeContext()
        result = ScanResult(started=time.time())
        chosen = self.selected(ctx)

        runnable: list[Backend] = []
        for b in chosen:
            try:
                ok, why = b.available(ctx)
            except Exception as e:                        # a broken availability check
                result.reports.append(
                    BackendReport(b.name, False, reason=f"availability check failed: {e}")
                )
                continue
            if not ok:
                result.reports.append(BackendReport(b.name, False, reason=why))
                continue
            runnable.append(b)

        if not runnable:
            result.finished = time.time()
            return result

        with ThreadPoolExecutor(max_workers=min(12, len(runnable))) as pool:
            futures = {pool.submit(self._run_one, b, ctx): b for b in runnable}
            # Give the pool a little slack over the per-backend budget so the
            # backend's own timeout fires first and we get a useful message.
            deadline = ctx.timeout + 5
            try:
                for fut in as_completed(futures, timeout=deadline):
                    report, devices = fut.result()
                    result.reports.append(report)
                    result.devices.extend(devices)
            except TimeoutError:
                done = {r.name for r in result.reports}
                for b in runnable:
                    if b.name not in done:
                        result.reports.append(
                            BackendReport(
                                b.name, True, ok=False,
                                error=f"exceeded {deadline:.0f}s scan deadline",
                                duration=deadline,
                            )
                        )

        result.devices.sort(key=lambda d: (str(d.kind), status_weight(d.status), d.uid))
        result.reports.sort(key=lambda r: r.name)
        result.finished = time.time()
        return result

    @staticmethod
    def _run_one(backend: Backend, ctx: ProbeContext) -> tuple[BackendReport, list[Device]]:
        t0 = time.perf_counter()
        try:
            devices = backend.probe(ctx) or []
        except Exception as e:
            return (
                BackendReport(
                    backend.name, True, ok=False,
                    error=f"{type(e).__name__}: {e}",
                    duration=time.perf_counter() - t0,
                ),
                [],
            )
        dt = time.perf_counter() - t0
        return BackendReport(backend.name, True, ok=True, duration=dt, count=len(devices)), devices


def build_scanner() -> Scanner:
    """Assemble the default backend set. Import here to keep startup lazy-ish."""
    from ..backends import all_backends

    scanner = Scanner()
    for backend in all_backends():
        scanner.register(backend)
    return scanner


def format_exception() -> str:  # pragma: no cover - used by --debug paths
    return traceback.format_exc()
