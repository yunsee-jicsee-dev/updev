"""Learned reflexes: the output side of the mushroom body.

The fly already recognises states. A mushroom body that only recognises is
half a circuit — its output neurons exist to *drive behaviour*, and that is
the whole point of having learned the state in the first place. MBONs are
premotor: recognise the smell, pick the action.

So a reflex is one binding, `state → command`. The fly watches, names the
state it is in, and runs what it was taught for that state. It is not writing
the command; a person writes it once. What the fly contributes is the thing it
is actually good at — deciding *when*.

Four rules hold this together, and they are all about the fact that running
commands is not like reading sysfs:

  1. **Innate beats learned.** If the lateral horn is alarming, no reflex
     fires, whatever the recognition says. A board in trouble is the worst
     possible moment to run something automatically, and the unlearnable path
     already exists precisely to outrank training.

  2. **Confidence is required.** A recognition that cannot separate two states
     is not a basis for action. Reflexes fire only on `Recognition.confident`,
     the same margin test `fly states` reports.

  3. **Edges, not levels.** A reflex fires when the board *enters* a state,
     not for as long as it stays there. Otherwise a watch loop re-runs the
     command every few seconds forever.

  4. **Disarmed by default.** A stored reflex does nothing until it is armed,
     and arming is a separate explicit act. Teaching the fly a command and
     letting it run are different decisions and are kept that way.

Nothing here decides a command is safe. The command is whatever the person
wrote; this module decides *when* it runs and refuses in the cases above.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Reflex",
    "ReflexBook",
    "Firing",
    "why_not",
]


#: Ceiling on how long a reflex may run before it is killed. A reflex is meant
#: to be a quick reaction — log a line, flip a GPIO, kick a script. Anything
#: that wants minutes should be a service the reflex starts, not the reflex.
DEFAULT_TIMEOUT = 30.0

#: Shortest gap between two firings of the same reflex. Guards the case where
#: a board flickers between two states: the recognition is genuinely changing,
#: but running the command ten times a minute is never what was meant.
REFUSAL_WINDOW = 10.0


@dataclass(slots=True)
class Reflex:
    """One `state → command` binding."""

    state: str
    command: str
    armed: bool = False
    timeout: float = DEFAULT_TIMEOUT
    runs: int = 0
    last_fired: float = 0.0
    last_status: int | None = None
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "command": self.command,
            "armed": self.armed,
            "timeout": self.timeout,
            "runs": self.runs,
            "last_fired": self.last_fired,
            "last_status": self.last_status,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reflex":
        return cls(
            state=str(data.get("state") or ""),
            command=str(data.get("command") or ""),
            armed=bool(data.get("armed")),
            timeout=float(data.get("timeout") or DEFAULT_TIMEOUT),
            runs=int(data.get("runs") or 0),
            last_fired=float(data.get("last_fired") or 0.0),
            last_status=(None if data.get("last_status") is None
                         else int(data["last_status"])),
            created=float(data.get("created") or time.time()),
        )


@dataclass(slots=True)
class Firing:
    """What happened when a reflex was (or was not) run."""

    reflex: Reflex
    ran: bool
    refused: str = ""               # why it did not run, if it did not
    status: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.ran and self.status == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.reflex.state,
            "command": self.reflex.command,
            "ran": self.ran,
            "refused": self.refused,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
            "dry_run": self.dry_run,
            "ok": self.ok,
        }


def why_not(reflex: Reflex, verdict, previous_state: str, now: float = 0.0) -> str:
    """The reason this reflex must not fire, or "" if it may.

    Written as a single function returning a reason rather than a boolean so
    that a refusal can always be explained to the person watching. A reflex
    that silently does nothing is indistinguishable from a broken one.
    """
    now = now or time.time()

    if not reflex.armed:
        return "무장되지 않음 — updev fly reflex arm 으로 켜세요"

    if verdict.alarms:
        channels = ", ".join(sorted({a.channel for a in verdict.alarms}))
        return f"측면뿔 경보 중({channels}) — 경보 상태에서는 어떤 반사도 실행하지 않습니다"

    recognition = verdict.recognition
    if recognition.label != reflex.state:
        return ""                    # not this reflex's state; not a refusal

    if not recognition.confident:
        return (f"'{reflex.state}' 로 보이지만 확실하지 않습니다 "
                f"(격차 {recognition.margin:.3f}) — 애매할 때는 실행하지 않습니다")

    if previous_state == reflex.state:
        return "이미 이 상태였습니다 — 반사는 상태에 들어설 때 한 번만 반응합니다"

    if reflex.last_fired and now - reflex.last_fired < REFUSAL_WINDOW:
        wait = REFUSAL_WINDOW - (now - reflex.last_fired)
        return f"{wait:.0f}초 전에 실행했습니다 — 연타 방지"

    return ""


@dataclass
class ReflexBook:
    """The set of reflexes, and the running of them.

    Kept beside the brain rather than inside it: weights are what the fly
    learned, reflexes are what a person decided it should do about that. They
    have different lifetimes — changing the glomeruli invalidates the weights
    but leaves the bindings perfectly meaningful.
    """

    reflexes: dict[str, Reflex] = field(default_factory=dict)

    # -- editing -----------------------------------------------------------

    def teach(self, state: str, command: str, timeout: float = DEFAULT_TIMEOUT) -> Reflex:
        """Bind a command to a state. Always lands disarmed."""
        reflex = Reflex(state=state, command=command, timeout=timeout)
        existing = self.reflexes.get(state)
        if existing is not None:
            # Keep the history; the binding changed, the reflex did not.
            reflex.runs = existing.runs
            reflex.created = existing.created
        self.reflexes[state] = reflex
        return reflex

    def arm(self, state: str, on: bool = True) -> Reflex | None:
        reflex = self.reflexes.get(state)
        if reflex is not None:
            reflex.armed = on
        return reflex

    def drop(self, state: str) -> bool:
        return self.reflexes.pop(state, None) is not None

    # -- firing ------------------------------------------------------------

    def consider(self, verdict, previous_state: str,
                 dry_run: bool = True) -> Firing | None:
        """Decide whether the current verdict should fire a reflex, and do it.

        Returns None when no reflex is bound to the recognised state at all —
        the ordinary case, and not worth reporting. A bound reflex that was
        refused comes back as a `Firing` carrying the reason.
        """
        label = verdict.recognition.label
        reflex = self.reflexes.get(label)
        if reflex is None:
            return None

        refusal = why_not(reflex, verdict, previous_state)
        if refusal:
            return Firing(reflex=reflex, ran=False, refused=refusal, dry_run=dry_run)

        if dry_run:
            return Firing(reflex=reflex, ran=False,
                          refused="dry-run — 실제로 실행하지 않았습니다",
                          dry_run=True)

        return self.run(reflex)

    @staticmethod
    def run(reflex: Reflex) -> Firing:
        """Execute the command. The only place in this module that does.

        Run through a shell because that is what the person wrote — a reflex
        command is a command line, pipes and redirects included, exactly as it
        would be typed. `shlex.split` would reject half of what is useful here
        and would not make anything safer: the command is already trusted at
        the moment it is taught.
        """
        started = time.perf_counter()
        env = dict(os.environ)
        env["UPDEV_REFLEX_STATE"] = reflex.state
        try:
            proc = subprocess.run(
                reflex.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=reflex.timeout,
                errors="replace",
                env=env,
            )
            status, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            status, out, err = 124, "", f"{reflex.timeout:.0f}초 제한을 넘겨 중단했습니다"
        except OSError as e:
            status, out, err = 126, "", str(e)

        reflex.runs += 1
        reflex.last_fired = time.time()
        reflex.last_status = status
        return Firing(
            reflex=reflex, ran=True, status=status,
            stdout=out.strip(), stderr=err.strip(),
            duration=time.perf_counter() - started,
        )

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {state: r.as_dict() for state, r in sorted(self.reflexes.items())}

    @classmethod
    def from_dict(cls, data: Any) -> "ReflexBook":
        if not isinstance(data, dict):
            return cls()
        book = cls()
        for state, raw in data.items():
            if isinstance(raw, dict):
                reflex = Reflex.from_dict({**raw, "state": state})
                if reflex.command:
                    book.reflexes[state] = reflex
        return book


def preview(command: str, width: int = 60) -> str:
    """One-line rendering of a command, for tables."""
    flat = " ".join(command.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def looks_destructive(command: str) -> list[str]:
    """Patterns worth warning about when a reflex is armed.

    Not a safety mechanism and not a filter — nothing here blocks anything.
    It exists so that arming a reflex that runs `dd` says so out loud, because
    the gap between "I typed this once" and "this now runs by itself" is where
    that distinction stops being obvious.
    """
    flat = f" {' '.join(command.split())} "
    hits: list[str] = []
    for needle, why in (
        ("rm -rf", "재귀 삭제"),
        ("rm -f", "강제 삭제"),
        ("dd ", "블록 장치 직접 쓰기"),
        ("mkfs", "포맷"),
        ("> /dev/", "장치 노드에 쓰기"),
        ("shutdown", "종료"),
        ("reboot", "재부팅"),
        ("systemctl", "서비스 제어"),
        ("chmod", "권한 변경"),
        ("chown", "소유권 변경"),
        (" sudo ", "권한 상승"),
    ):
        if needle in flat:
            hits.append(f"{needle.strip()} — {why}")
    return hits
