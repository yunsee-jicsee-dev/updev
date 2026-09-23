"""A fruit fly runs the device manager.

The *Drosophila* olfactory learning pathway solves, in about 2,000 neurons and
no floating-point hardware, the exact problem a device manager keeps failing
at: **is what I am looking at right now normal for this machine?**

Threshold rules can't answer it. "Warn above 80°C" doesn't know that *this*
Pi always runs at 78°C with the camera attached, and that 71°C at 3am with no
camera is the strange thing. The fly's answer is not a threshold — it is a
memory of smells. That machinery is worth stealing wholesale, because it is
tiny, it runs offline, and it learns from a handful of examples.

The circuit, as the connectome maps it, and what each stage does here:

    receptor neurons  →  51 glomeruli        one per feature of the scan
              ↓            (antennal lobe)   divisive gain control
    projection neurons →  concentration-invariant code
              ↓
    Kenyon cells      →  2,000 cells, each sampling ~6 glomeruli at random
              ↓            (mushroom body)   a random projection
    APL (1 neuron)    →  feedback inhibition, top 5% survive
              ↓                              → a 100-bit sparse tag
    MBONs             →  plastic synapses read the tag out as
                           MBON-α'3      novelty   (낯섦)
                           MBON-γ1pedc   avoidance (위험)
    DANs              →  dopamine from issue severity depresses what fired

    lateral horn      →  a parallel, hardwired path that never learns

The random projection into a sparse tag is the fly's locality-sensitive hash
(Dasgupta, Stevens & Navlakha, *Science* 2017): similar scans get similar
tags, so familiarity generalises instead of memorising exact states. The
lateral horn is the reason this is safe to put in charge of anything — innate
alarms bypass the mushroom body entirely, so no amount of training can teach
the fly to shrug at a full root filesystem.

Offline, always. The architecture and its cell counts come from the published
connectome; no synapse data is downloaded, fetched or required, and the only
dependency is the standard library. The random projection is seeded from a
fixed constant, so every board grows the identical wiring and a memory file
is portable between them.

What it is not: a simulation of the fly brain. No spikes, no time, no
biophysics. It is the *algorithm* the circuit implements, which is the part
that transfers.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .core.model import Kind, ScanResult, Severity, Status

__all__ = [
    "COMPARTMENT_SPECS",
    "GLOMERULI",
    "HEADLINE",
    "RECENT",
    "Alarm",
    "Compartment",
    "FlyBrain",
    "Mood",
    "Percept",
    "Recognition",
    "Verdict",
    "brain_path",
    "innate_alarms",
    "load_brain",
    "smell",
]


# --------------------------------------------------------------------------
# connectome constants
#
# Counts as the literature reports them for one hemisphere of the adult brain.
# They are the shape of the circuit, not a claim to have reproduced it.
# --------------------------------------------------------------------------

#: Glomeruli in the antennal lobe — the fly's input channels.
ANTENNAL_LOBE_GLOMERULI = 51

#: Kenyon cells in the mushroom body. Commonly cited as ~2,000 per hemisphere.
KENYON_CELLS = 2_000

#: Glomeruli each Kenyon cell samples ("claws"). Measured mean is about 6,
#: and — importantly — the sampling is random rather than stereotyped.
CLAWS_PER_KENYON_CELL = 6

#: Fraction of Kenyon cells that survive APL inhibition for any one odour.
#: The sparseness is enforced by a single giant inhibitory neuron per
#: hemisphere, which is why it is a clean winner-take-all and not a threshold.
SPARSITY = 0.05

#: Seed for the PN→KC projection. Fixed so that every install wires the same
#: brain and a trained memory file means the same thing on another board.
WIRING_SEED = 0x_F1_7B_12_A1

#: How far one exposure moves a synapse toward fully depressed.
LEARNING_RATE = 0.35


# --------------------------------------------------------------------------
# mushroom body compartments
#
# The mushroom body is not one memory. Its lobes are divided into compartments,
# each with its own dopaminergic neuron and its own output neuron, and they
# differ in how long what they learn survives: γ holds minutes, α'β' holds
# hours, α/β holds days. The same odour is written into all of them at once
# and fades out of them at different rates.
#
# That is the whole reason to have more than one. A single memory can only say
# "familiar" or "not". Three, read side by side, separate two situations that
# matter and that a single number conflates:
#
#   γ novel, α/β familiar  → this board, but not how it has been lately
#   γ familiar, α/β novel  → new, and it has been going on for a while now
#
# Decay is by wall clock rather than per exposure, so "short-term" means
# twenty minutes and not "three commands ago" — the timescales have to be
# real time to mean anything, since training happens whenever a person runs
# the command.
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Compartment:
    """One lobe compartment: a weight table, a learning rate, a half-life."""

    key: str
    title: str
    half_life: float                # seconds until a memory is half forgotten
    rate: float                     # how far one exposure depresses a synapse
    weights: dict[int, float] = field(default_factory=dict)

    def retention(self, elapsed: float) -> float:
        """How much of what was learned is left after `elapsed` seconds."""
        if elapsed <= 0 or not self.half_life:
            return 1.0
        return 0.5 ** (elapsed / self.half_life)

    def novelty(self, tag: tuple[int, ...], elapsed: float = 0.0) -> float:
        """Mean undepressed drive across the cells that fired.

        Decay enters as a single scalar because it applies uniformly to every
        synapse in the compartment — which means novelty can be read without
        rewriting the table, and reading stays free of side effects.
        """
        if not tag:
            return 1.0
        kept = self.retention(elapsed)
        return sum(1.0 - self.weights.get(k, 0.0) * kept for k in tag) / len(tag)

    def settle(self, elapsed: float) -> None:
        """Bake elapsed decay into the table. Only ever called before writing."""
        kept = self.retention(elapsed)
        if kept >= 1.0:
            return
        for k in list(self.weights):
            self.weights[k] *= kept
            if self.weights[k] < 0.01:
                del self.weights[k]

    def learn(self, tag: tuple[int, ...]) -> None:
        for k in tag:
            current = self.weights.get(k, 0.0)
            self.weights[k] = current + self.rate * (1.0 - current)


#: (key, title, half-life, learning rate), shortest-lived first.
#:
#: Short-term learns fastest and forgets fastest, which is the ordering the
#: biology shows and also the only one that behaves sensibly: if the long-term
#: compartment learned as fast as γ, a single odd scan would be remembered for
#: a month.
COMPARTMENT_SPECS: tuple[tuple[str, str, float, float], ...] = (
    ("gamma",     "γ 단기",    20 * 60,        0.45),
    ("alpha_beta_prime", "α'β' 중기", 12 * 3600, 0.35),
    ("alpha_beta", "αβ 장기",  30 * 86400,     0.25),
)

#: The compartment whose novelty is the headline number. Long-term answers
#: "is this board like this", which is the question `fly sniff` is asking.
HEADLINE = "alpha_beta"

#: The compartment that answers "has this changed lately".
RECENT = "gamma"

#: Retention below which the short-term compartment is carrying no information.
#:
#: γ forgets on wall clock, whether or not anyone was watching. So a board left
#: alone overnight comes back with an empty short-term memory — and reading
#: that as "this changed recently" would be exactly backwards: nothing changed,
#: nobody looked. Two half-lives is where γ stops being evidence and starts
#: being absence of evidence, and the drift reading is withheld below it.
STALE_RETENTION = 0.25

#: How far short-term novelty must exceed long-term before it is called drift.
#:
#: The state a board is currently living in sits a little *below* zero — γ
#: knows it better than α/β does, because γ learns faster. A state the board
#: knows well but has not been in lately lands well above. The gap between
#: those two is wide, so this sits in the middle of it rather than at the edge.
DRIFT_THRESHOLD = 0.20


def _fresh_compartments() -> dict[str, Compartment]:
    """A naive set of lobes. Ordered shortest-lived first, as displayed."""
    return {
        key: Compartment(key=key, title=title, half_life=half_life, rate=rate)
        for key, title, half_life, rate in COMPARTMENT_SPECS
    }


# --------------------------------------------------------------------------
# the 51 glomeruli
# --------------------------------------------------------------------------

#: Tags worth a dedicated receptor. Chosen because each one changes what the
#: machine *is*, not merely what it contains.
#:
#: `FUSB` is the USB floppy class from `usbclass.py`. It earns a channel over
#: the loop devices that used to sit here because a floppy drive is hardware
#: that comes and goes, while loop mounts are squashfs bookkeeping that never
#: moves — the fly should notice a drive appearing, not a snap being indexed.
#:
#: `usb-storage` used to sit here and no longer does: the storage backend puts
#: it on the block device while the USB backend puts `storage` on the device it
#: arrived on, so the two fire together and `bus:usb` already carries the rest.
#: The channel it freed is spent on `state:empty-bay`, which distinguishes
#: things this could not.
_TAG_GLOMERULI = (
    "hotplug", "storage", "input", "camera", "hub", "root-hub",
    "nvme", "sd-card", "read-only", "FUSB", "wifi",
)

_BUS_GLOMERULI = ("usb", "i2c", "spi", "serial", "net", "block")


def _build_glomeruli() -> tuple[str, ...]:
    """Name every input channel, once, in a fixed order.

    Order matters: it is the axis of the random projection. Adding a `Kind`
    upstream changes this list, which changes the wiring — which is exactly
    why the signature below is stored alongside a trained memory.
    """
    names = [f"kind:{k}" for k in Kind]
    names += [f"status:{s}" for s in Status]
    names += [f"sev:{s}" for s in Severity]
    names += [f"tag:{t}" for t in _TAG_GLOMERULI]
    names += [f"bus:{b}" for b in _BUS_GLOMERULI]
    names += ["census:population", "census:issues", "census:backends-down"]
    names += ["state:usb-degraded", "state:fs-pressure", "state:thermal",
              "state:empty-bay"]
    return tuple(names)


GLOMERULI: tuple[str, ...] = _build_glomeruli()


def glomerulus_signature() -> str:
    """Stable fingerprint of the input layer, stored with a trained memory.

    A memory trained against a different set of glomeruli is not wrong so much
    as meaningless — the bits no longer refer to the same things.
    """
    import hashlib

    return hashlib.sha256("\n".join(GLOMERULI).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# antennal lobe — a scan becomes a smell
# --------------------------------------------------------------------------

def _saturate(count: float, half: float) -> float:
    """Receptor response: 0 at nothing, 0.5 at `half`, asymptotic to 1.

    Counts have no ceiling but firing rates do, so a raw count cannot be fed
    to a neuron. Saturation also buys the right sensitivity curve for free —
    the step from 0 issues to 1 matters enormously, from 30 to 31 not at all.
    """
    return count / (count + half) if count > 0 else 0.0


def _raw_activation(result: ScanResult) -> dict[str, float]:
    """Receptor-level response of all 51 glomeruli to one scan."""
    devices = result.devices
    total = max(len(devices), 1)
    raw = dict.fromkeys(GLOMERULI, 0.0)

    for dev in devices:
        raw[f"kind:{dev.kind}"] = raw.get(f"kind:{dev.kind}", 0.0) + 1.0
        raw[f"status:{dev.status}"] = raw.get(f"status:{dev.status}", 0.0) + 1.0
        for tag in dev.tags:
            key = f"tag:{tag}"
            if key in raw:
                raw[key] += 1.0
        bus = (dev.bus or "").lower()
        for name in _BUS_GLOMERULI:
            if bus.startswith(name):
                raw[f"bus:{name}"] += 1.0
                break
        for issue in dev.issues:
            raw[f"sev:{issue.severity}"] += 1.0

    # Kind, status, tag and bus channels are fractions of the population, so a
    # 40-device board and a 12-device board smell alike when they are alike.
    for name in GLOMERULI:
        if name.startswith(("kind:", "status:", "tag:", "bus:")):
            raw[name] /= total

    # Severity is deliberately *not* normalised by population — three errors
    # are three errors whether the board has four devices or ninety.
    for sev in Severity:
        raw[f"sev:{sev}"] = _saturate(raw[f"sev:{sev}"], half=3.0)

    raw["census:population"] = _saturate(len(devices), half=25.0)
    raw["census:issues"] = _saturate(sum(len(d.issues) for d in devices), half=5.0)
    raw["census:backends-down"] = _saturate(
        sum(1 for r in result.reports if r.available and not r.ok), half=1.5
    )

    raw["state:usb-degraded"] = _saturate(
        sum(1 for d in devices
            if d.kind is Kind.USB and d.status is Status.DEGRADED),
        half=1.5,
    )
    # Removable drives sitting empty. A floppy or card reader keeps its block
    # device when the medium leaves — same node, same tags, same status, only
    # the capacity goes to zero. Nothing else here reads capacity, so without
    # this channel ejecting a disk is completely invisible to the fly: the two
    # scans produce byte-identical tags.
    raw["state:empty-bay"] = _saturate(
        sum(1 for d in devices
            if d.kind is Kind.STORAGE
            and d.metrics.get("size_bytes", -1.0) == 0.0),
        half=1.0,
    )

    raw["state:fs-pressure"] = max(
        (d.metrics.get("fs_used_pct", 0.0) / 100.0 for d in devices), default=0.0
    )
    raw["state:thermal"] = min(
        1.0,
        max((d.metrics.get("temp_c", 0.0) for d in devices), default=0.0) / 85.0,
    )
    return raw


def _project(raw: dict[str, float]) -> list[float]:
    """Antennal lobe gain control: the ORN→PN transform.

    Divisive normalisation by total input (Olsen, Bhandawat & Wilson, 2010).
    Its purpose in the fly is concentration invariance — a faint smell and a
    strong one of the same thing must produce the same downstream code. Here
    it stops a busy board from lighting up every Kenyon cell simply by being
    busy, which would make "lots of devices" the only thing the fly ever
    noticed.
    """
    values = [raw[name] for name in GLOMERULI]
    total = sum(values)
    sigma = 0.12                       # spontaneous drive; keeps a silent scan finite
    scale = sigma + 1.5 * (total / len(values))
    return [v / scale for v in values]


# --------------------------------------------------------------------------
# mushroom body — the random projection and its sparse tag
# --------------------------------------------------------------------------

def _wire_claws() -> tuple[tuple[int, ...], ...]:
    """Grow the PN→KC connections: each Kenyon cell picks ~6 glomeruli.

    Random, not stereotyped — that is the finding that makes the mushroom body
    a hash rather than a labelled line, and it is what lets the same circuit
    encode smells evolution never met.
    """
    rng = random.Random(WIRING_SEED)
    n = len(GLOMERULI)
    return tuple(
        tuple(rng.sample(range(n), CLAWS_PER_KENYON_CELL))
        for _ in range(KENYON_CELLS)
    )


#: Grown once at import. ~12,000 small ints; cheap to hold, expensive to redo.
CLAWS: tuple[tuple[int, ...], ...] = _wire_claws()

#: Kenyon cells that survive APL inhibition. 5% of 2,000 = a 100-bit tag.
TAG_BITS = max(1, int(KENYON_CELLS * SPARSITY))


def _kenyon_tag(pn: list[float]) -> tuple[int, ...]:
    """Sum each cell's claws, then let APL silence all but the strongest 5%.

    One inhibitory neuron, fed by the whole mushroom body and projecting back
    onto all of it, implements a winner-take-all — the sparsity is a property
    of the circuit, not a tuned threshold, which is why it holds across
    wildly different inputs.
    """
    sums = [sum(pn[i] for i in claws) for claws in CLAWS]
    order = sorted(range(KENYON_CELLS), key=lambda k: sums[k], reverse=True)
    return tuple(sorted(order[:TAG_BITS]))


# --------------------------------------------------------------------------
# percept
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Percept:
    """One scan, as the fly experiences it."""

    raw: dict[str, float]           # receptor response per glomerulus
    pn: list[float]                 # after gain control
    tag: tuple[int, ...]            # the Kenyon cells that fired

    @property
    def strongest(self) -> list[tuple[str, float]]:
        """Which glomeruli dominate this smell — the fly's own explanation."""
        pairs = [(n, v) for n, v in self.raw.items() if v > 0.001]
        pairs.sort(key=lambda p: p[1], reverse=True)
        return pairs[:8]

    def overlap(self, other: "Percept") -> float:
        """Shared Kenyon cells, 0–1. The similarity the hash was built for."""
        if not self.tag or not other.tag:
            return 0.0
        return len(set(self.tag) & set(other.tag)) / len(self.tag)


def smell(result: ScanResult) -> Percept:
    """Turn a scan into a sparse tag. Pure, and the only entry point."""
    raw = _raw_activation(result)
    pn = _project(raw)
    return Percept(raw=raw, pn=pn, tag=_kenyon_tag(pn))


# --------------------------------------------------------------------------
# lateral horn — innate, unlearnable
# --------------------------------------------------------------------------

class Mood(StrEnum):
    """What the fly decides to do about a scan."""

    SETTLED = "settled"      # familiar and benign — nothing to do
    CURIOUS = "curious"      # mildly novel — worth a closer look
    DRIFTED = "drifted"      # the board it has always been, but not lately
    STARTLED = "startled"    # strongly novel — say so, offer to learn it
    AVERSIVE = "aversive"    # familiar, and it has gone badly before
    ALARMED = "alarmed"      # lateral horn fired; training is irrelevant


MOOD_LABEL: dict[Mood, str] = {
    Mood.SETTLED: "익숙함 — 평소의 이 보드",
    Mood.CURIOUS: "조금 낯섦 — 살펴볼 만함",
    Mood.DRIFTED: "달라짐 — 이 보드는 맞는데 최근 모습이 아님",
    Mood.STARTLED: "낯섦 — 처음 맡는 냄새",
    Mood.AVERSIVE: "위험한 냄새 — 전에 문제가 있었음",
    Mood.ALARMED: "경보 — 학습과 무관하게 잘못됨",
}

MOOD_STYLE: dict[Mood, str] = {
    Mood.SETTLED: "green",
    Mood.CURIOUS: "cyan",
    Mood.DRIFTED: "yellow",
    Mood.STARTLED: "bright_yellow",
    Mood.AVERSIVE: "magenta",
    Mood.ALARMED: "bright_red",
}


@dataclass(slots=True)
class Alarm:
    """A hardwired response. Carries its own reason; never learned away."""

    channel: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"channel": self.channel, "message": self.message, "detail": self.detail}


def innate_alarms(result: ScanResult) -> list[Alarm]:
    """The lateral horn: responses wired in, bypassing the mushroom body.

    A fly does not need to have met a wasp to flee one. Everything here is a
    condition where "you have seen this a hundred times" is not a defence —
    so none of it consults the trained memory, and no training can suppress it.
    """
    alarms: list[Alarm] = []

    errors = [
        (d, i) for d in result.devices for i in d.issues
        if i.severity is Severity.ERROR
    ]
    for dev, issue in errors:
        alarms.append(Alarm("error", f"{dev.label}: {issue.message}", issue.fix))

    dead = [r for r in result.reports if r.available and not r.ok]
    for report in dead:
        alarms.append(
            Alarm("backend", f"{report.name} 백엔드가 실패했습니다", report.error)
        )

    hot = [(d, d.metrics["temp_c"]) for d in result.devices if d.metrics.get("temp_c", 0) >= 80]
    for dev, temp in hot:
        alarms.append(Alarm("thermal", f"{dev.label} {temp:.0f}°C", "냉각을 확인하세요"))

    full = [
        (d, d.metrics["fs_used_pct"]) for d in result.devices
        if d.metrics.get("fs_used_pct", 0) >= 95
    ]
    for dev, pct in full:
        alarms.append(Alarm("storage", f"{dev.label} {pct:.0f}% 사용 중", "공간을 확보하세요"))

    return alarms


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Recognition:
    """Which named state this smells like, if any.

    Supervised learning on the same sparse tag. The unsupervised half of the
    mushroom body asks "have I smelled this before"; naming states asks the
    harder and more useful question, "which of the things I know is it".
    """

    label: str = ""
    score: float = 0.0              # mean learned weight over the firing cells
    margin: float = 0.0             # lead over the runner-up
    runners: list[tuple[str, float]] = field(default_factory=list)

    #: Below this the best match is not worth reporting — the cells that fired
    #: simply have not been associated with any named state.
    FLOOR = 0.25

    #: A win this narrow means two states genuinely overlap, and saying which
    #: one it is would be making it up.
    CLEAR_MARGIN = 0.08

    @property
    def confident(self) -> bool:
        return bool(self.label) and self.score >= self.FLOOR \
            and self.margin >= self.CLEAR_MARGIN

    @property
    def summary(self) -> str:
        if not self.label or self.score < self.FLOOR:
            return "이름 붙은 상태 중에는 해당 없음"
        if not self.confident:
            others = ", ".join(n for n, _ in self.runners[:1])
            return f"{self.label} (확실치 않음 — {others} 와 구분이 안 됨)"
        return self.label

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "margin": round(self.margin, 4),
            "confident": self.confident,
            "summary": self.summary,
            "runners": [{"label": n, "score": round(s, 4)} for n, s in self.runners],
        }


@dataclass(slots=True)
class Verdict:
    """What the fly makes of a scan, with the trail that got it there."""

    mood: Mood
    novelty: float                  # headline: long-term compartment, 0–1
    aversion: float                 # MBON-γ1pedc output, 0 – 1
    exposures: int                  # training events behind this judgement
    percept: Percept
    compartments: dict[str, float] = field(default_factory=dict)  # key → novelty
    recognition: Recognition = field(default_factory=Recognition)
    alarms: list[Alarm] = field(default_factory=list)
    attend: list[str] = field(default_factory=list)   # glomeruli driving the surprise

    @property
    def label(self) -> str:
        return MOOD_LABEL[self.mood]

    #: False when the short-term compartment has decayed past usefulness, in
    #: which case `drift` is withheld rather than guessed at. See STALE_RETENTION.
    recent_fresh: bool = True

    @property
    def drift(self) -> float:
        """How much more novel this is to the short term than the long term.

        Positive means the board still looks like itself over weeks but has
        changed over the last few minutes — the reading that a single
        familiarity number cannot express.

        Zero when the short-term memory is stale, because "I have not looked
        in an hour" and "this changed in the last few minutes" are different
        facts and only one of them is worth acting on.
        """
        if not self.compartments or not self.recent_fresh:
            return 0.0
        return self.compartments.get(RECENT, 0.0) - self.compartments.get(HEADLINE, 0.0)

    @property
    def should_learn(self) -> bool:
        """Novel, but nothing innately wrong — the case worth imprinting."""
        return self.mood in (Mood.STARTLED, Mood.DRIFTED) and not self.alarms

    def as_dict(self) -> dict[str, Any]:
        return {
            "mood": str(self.mood),
            "label": self.label,
            "novelty": round(self.novelty, 4),
            "aversion": round(self.aversion, 4),
            "drift": round(self.drift, 4),
            "compartments": {k: round(v, 4) for k, v in self.compartments.items()},
            "recognition": self.recognition.as_dict(),
            "exposures": self.exposures,
            "attend": self.attend,
            "alarms": [a.as_dict() for a in self.alarms],
            "strongest": [{"glomerulus": n, "response": round(v, 4)}
                          for n, v in self.percept.strongest],
            "tag_bits": len(self.percept.tag),
        }


# --------------------------------------------------------------------------
# the brain
# --------------------------------------------------------------------------

def _invoking_home() -> Path:
    """The home of whoever typed the command, even under sudo.

    updev is commonly aliased to `sudo updev`, and `Path.home()` under sudo is
    /root. That would give one brain for the alias and another for a bare
    `python3 -m updev`, each convinced the other's board is unfamiliar. The
    memory belongs to the person, not to the privilege level.
    """
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            import pwd

            return Path(pwd.getpwnam(user).pw_dir)
        except (KeyError, ImportError, OSError):
            pass
    return Path.home()


def brain_path() -> Path:
    """Where the trained memory lives. XDG, with the usual fallback."""
    base = os.environ.get("XDG_STATE_HOME") or str(_invoking_home() / ".local" / "state")
    return Path(base) / "updev" / "flybrain.json"


def reflex_path(brain: Path | None = None) -> Path:
    """Reflexes live beside the brain, not inside it.

    Different lifetimes. Weights are what the fly learned and are invalidated
    whenever the glomeruli change; a `state → command` binding survives that
    perfectly well, and losing someone's commands because a receptor was added
    would be indefensible.
    """
    brain = brain or brain_path()
    return brain.with_name(brain.stem + "-reflexes.json")


def _restore_ownership(path: Path) -> None:
    """Hand a file written under sudo back to the user who invoked it.

    Without this the first `sudo updev fly learn` leaves a root-owned memory
    in the user's home, and every later run without sudo fails to save.
    """
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return
    try:
        import pwd

        entry = pwd.getpwnam(user)
        for target in (path, path.parent):
            os.chown(target, entry.pw_uid, entry.pw_gid)
    except (KeyError, ImportError, OSError):
        pass


@dataclass
class FlyBrain:
    """Mushroom body output synapses, and the training that shapes them.

    Two plastic populations, both keyed by Kenyon cell:

    `familiar` is MBON-α'3, the novelty compartment. Synapses start at 0 —
    a naive fly finds everything novel — and are depressed toward 1 by
    exposure. Novelty is what the firing cells have *not yet* learned.

    `aversive` is MBON-γ1pedc>α/β, the aversive-memory compartment. It is
    written only when dopamine arrives, and dopamine here means the scan
    carried real issues. It is how "this shape of machine goes wrong" becomes
    a smell in its own right, separate from whether the shape is familiar.
    """

    compartments: dict[str, Compartment] = field(default_factory=_fresh_compartments)
    aversive: dict[int, float] = field(default_factory=dict)
    states: dict[str, dict[int, float]] = field(default_factory=dict)
    exposures: int = 0
    created: float = field(default_factory=time.time)
    updated: float = 0.0
    signature: str = field(default_factory=glomerulus_signature)

    #: Why this brain hatched empty, when it did so by discarding something.
    #: Not persisted — it describes this load, not the memory. A fly that
    #: silently forgets everything looks broken; one that says the receptors
    #: changed is merely inconvenient.
    reset_reason: str = ""
    lost_states: list[str] = field(default_factory=list)

    # -- readout -----------------------------------------------------------

    def _elapsed(self, now: float | None = None) -> float:
        """Seconds of forgetting owed since the last write."""
        if not self.updated:
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self.updated)

    def novelty(self, percept: Percept, compartment: str = HEADLINE) -> float:
        """MBON-α'3, read from one compartment."""
        comp = self.compartments.get(compartment)
        if comp is None:
            return 1.0
        return comp.novelty(percept.tag, self._elapsed())

    def novelties(self, percept: Percept) -> dict[str, float]:
        """All three at once — the reading a single number cannot give."""
        elapsed = self._elapsed()
        return {key: comp.novelty(percept.tag, elapsed)
                for key, comp in self.compartments.items()}

    def aversion(self, percept: Percept) -> float:
        """MBON-γ1pedc: mean aversive weight across the cells that fired."""
        if not percept.tag:
            return 0.0
        return sum(self.aversive.get(k, 0.0) for k in percept.tag) / len(percept.tag)

    def recognize(self, percept: Percept) -> Recognition:
        """Which named state this smells most like.

        Every state gets scored, not just the winner, because the margin is
        what decides whether the answer is worth saying out loud — two states
        that share most of their tag are genuinely ambiguous, and picking one
        would be inventing precision.
        """
        if not percept.tag or not self.states:
            return Recognition()
        scored = [
            (name, sum(table.get(k, 0.0) for k in percept.tag) / len(percept.tag))
            for name, table in self.states.items()
        ]
        scored.sort(key=lambda p: p[1], reverse=True)
        best, score = scored[0]
        margin = score - scored[1][1] if len(scored) > 1 else score
        return Recognition(label=best, score=score, margin=margin, runners=scored[1:4])

    def judge(self, result: ScanResult) -> Verdict:
        """Smell a scan and decide what to do about it.

        Order is the point. The lateral horn is consulted first and wins
        outright, because the whole reason for having an unlearnable path is
        that it outranks whatever the memory believes.
        """
        percept = smell(result)
        alarms = innate_alarms(result)
        novelties = self.novelties(percept)
        novelty = novelties.get(HEADLINE, 1.0)
        recent = novelties.get(RECENT, 1.0)
        aversion = self.aversion(percept)

        short = self.compartments.get(RECENT)
        fresh = bool(short) and short.retention(self._elapsed()) >= STALE_RETENTION

        if alarms:
            mood = Mood.ALARMED
        elif aversion >= 0.35:
            mood = Mood.AVERSIVE
        elif novelty >= 0.6:
            mood = Mood.STARTLED
        elif fresh and recent - novelty >= DRIFT_THRESHOLD:
            # The board still reads as itself over weeks, but not over the last
            # few minutes. Reported on its own because the fix is different:
            # nothing is wrong with the board, something about it just changed.
            mood = Mood.DRIFTED
        elif novelty >= 0.3:
            mood = Mood.CURIOUS
        else:
            mood = Mood.SETTLED

        return Verdict(
            mood=mood,
            novelty=novelty,
            aversion=aversion,
            exposures=self.exposures,
            percept=percept,
            compartments=novelties,
            recognition=self.recognize(percept),
            recent_fresh=fresh,
            alarms=alarms,
            attend=self._attend(percept),
        )

    def _attend(self, percept: Percept) -> list[str]:
        """Central complex: where to point the next scan.

        The glomeruli carrying the most drive into the cells the memory has
        least to say about. Not "what is loudest" — what is loud *and*
        unaccounted for, which is the only useful definition of suspicious.
        """
        long_term = self.compartments.get(HEADLINE)
        if long_term is None:
            return []
        kept = long_term.retention(self._elapsed())
        unlearned = [k for k in percept.tag
                     if long_term.weights.get(k, 0.0) * kept < 0.5]
        if not unlearned:
            return []
        scores: dict[int, float] = {}
        for k in unlearned:
            for i in CLAWS[k]:
                scores[i] = scores.get(i, 0.0) + percept.pn[i]
        ranked = sorted(scores.items(), key=lambda p: p[1], reverse=True)
        return [GLOMERULI[i] for i, v in ranked[:4] if v > 0.001]

    # -- plasticity --------------------------------------------------------

    def learn(self, result: ScanResult, dopamine: float | None = None,
              state: str = "") -> Verdict:
        """One training exposure. Returns the verdict from *before* learning.

        Returning the prior verdict is deliberate: after training, every scan
        looks familiar, so a post-hoc judgement would tell you nothing about
        what you just taught it.

        `state` names what is being taught. Naming is additive — the same
        exposure still writes the unsupervised compartments, so a named state
        is also a familiar one.
        """
        verdict = self.judge(result)
        percept = verdict.percept

        if dopamine is None:
            dopamine = _dopamine(result)

        # Settle the clock before writing: every compartment forgets what it
        # owes, and only then does the new exposure go in. Doing it the other
        # way round would decay what we just taught it.
        elapsed = self._elapsed()
        for comp in self.compartments.values():
            comp.settle(elapsed)
            comp.learn(percept.tag)

        if dopamine > 0:
            for k in percept.tag:
                prior = self.aversive.get(k, 0.0)
                self.aversive[k] = prior + dopamine * LEARNING_RATE * (1.0 - prior)

        if state:
            table = self.states.setdefault(state, {})
            for k in percept.tag:
                current = table.get(k, 0.0)
                table[k] = current + LEARNING_RATE * (1.0 - current)

        self.exposures += 1
        self.updated = time.time()
        return verdict

    def forget(self, state: str = "") -> bool:
        """Wipe everything, or just one named state. False if there was no
        such state to forget."""
        if state:
            return self.states.pop(state, None) is not None
        self.compartments = _fresh_compartments()
        self.aversive.clear()
        self.states.clear()
        self.exposures = 0
        self.created = time.time()
        self.updated = 0.0
        return True

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        elapsed = self._elapsed()
        headline = self.compartments.get(HEADLINE)
        touched = len(headline.weights) if headline else 0
        return {
            "glomeruli": len(GLOMERULI),
            "kenyon_cells": KENYON_CELLS,
            "claws_per_cell": CLAWS_PER_KENYON_CELL,
            "tag_bits": TAG_BITS,
            "exposures": self.exposures,
            "cells_touched": touched,
            "coverage": round(touched / KENYON_CELLS, 4),
            "aversive_cells": len(self.aversive),
            "compartments": [
                {
                    "key": comp.key,
                    "title": comp.title,
                    "half_life": comp.half_life,
                    "rate": comp.rate,
                    "cells": len(comp.weights),
                    "retention": round(comp.retention(elapsed), 4),
                }
                for comp in self.compartments.values()
            ],
            "states": {name: len(table) for name, table in sorted(self.states.items())},
            "created": self.created,
            "updated": self.updated,
            "signature": self.signature,
            "stale": self.signature != glomerulus_signature(),
        }

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "signature": self.signature,
            "wiring_seed": WIRING_SEED,
            "kenyon_cells": KENYON_CELLS,
            "exposures": self.exposures,
            "created": self.created,
            "updated": self.updated,
            "compartments": {
                key: {str(k): round(v, 5) for k, v in comp.weights.items()}
                for key, comp in self.compartments.items()
            },
            "aversive": {str(k): round(v, 5) for k, v in self.aversive.items()},
            "states": {
                name: {str(k): round(v, 5) for k, v in table.items()}
                for name, table in self.states.items()
            },
        }

    def save(self, path: Path | None = None) -> Path:
        """Write atomically — a half-written brain is worse than none."""
        path = path or brain_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        _restore_ownership(path)
        return path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlyBrain":
        brain = cls(
            aversive={int(k): float(v) for k, v in (data.get("aversive") or {}).items()},
            states={
                str(name): {int(k): float(v) for k, v in (table or {}).items()}
                for name, table in (data.get("states") or {}).items()
            },
            exposures=int(data.get("exposures") or 0),
            created=float(data.get("created") or time.time()),
            updated=float(data.get("updated") or 0.0),
            signature=str(data.get("signature") or ""),
        )

        stored = data.get("compartments")
        if isinstance(stored, dict):
            for key, table in stored.items():
                comp = brain.compartments.get(key)
                if comp is not None:
                    comp.weights = {int(k): float(v) for k, v in (table or {}).items()}
            return brain

        # Version 1 kept one undifferentiated `familiar` table. Seed every
        # compartment from it rather than discarding the training: what it
        # recorded is true of all three timescales at the moment it was
        # written, and the half-lives sort out the rest from here on.
        legacy = data.get("familiar")
        if isinstance(legacy, dict):
            weights = {int(k): float(v) for k, v in legacy.items()}
            for comp in brain.compartments.values():
                comp.weights = dict(weights)
        return brain


def _dopamine(result: ScanResult) -> float:
    """Reinforcement from the scan itself: how badly is this going?

    Dopaminergic neurons in the mushroom body carry punishment signals. The
    natural punishment for a device manager is the issue list, weighted by
    severity, so the fly learns a shape of machine *and* whether that shape
    tends to be in trouble.
    """
    score = 0.0
    for dev in result.devices:
        for issue in dev.issues:
            score += {Severity.ERROR: 1.0, Severity.WARN: 0.35, Severity.INFO: 0.0}[
                issue.severity
            ]
    return min(1.0, score / 3.0)


def load_brain(path: Path | None = None) -> FlyBrain:
    """Load a trained brain, or hatch a naive one. Never raises.

    A corrupt or stale memory file is treated as no memory at all. The failure
    mode of a fly that finds everything novel is noise; the failure mode of one
    reading garbage weights is confident nonsense.
    """
    path = path or brain_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return FlyBrain()
    if not isinstance(data, dict):
        return FlyBrain()
    brain = FlyBrain.from_dict(data)
    if brain.signature != glomerulus_signature():
        # The input layer changed under it. Keep the file for inspection, but
        # hatch fresh — old weights index Kenyon cells that now mean something
        # else entirely, including the ones behind every named state.
        fresh = FlyBrain()
        fresh.created = brain.created
        fresh.reset_reason = (
            f"입력 계층이 바뀌어 이전 기억({brain.exposures}회)을 쓸 수 없습니다 — "
            "같은 케니언세포가 다른 것을 뜻하게 되었습니다. 다시 학습시키세요."
        )
        fresh.lost_states = sorted(brain.states)
        return fresh
    return brain
