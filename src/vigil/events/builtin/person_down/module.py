"""The person_down module: a tracked person whose box geometry has collapsed (06 §11).

**This module is geometry. It is not pose estimation, and it says nothing about injury.**

That sentence is the design. A bounding box can show that something which was tall and moving
has become short, wide and still — and that is genuinely useful, because it is what a fall looks
like from a camera. What it cannot show is *why*, and the words this module is allowed to use
stop at the observation. `PERSON_DOWN` means "a person's posture appears to have collapsed and
stayed collapsed"; it does not mean injured, unconscious or in medical distress, and nothing
here, in the descriptor, or in the explanation an operator reads says otherwise.

No pose capability is declared, because none is used. 10-roadmap §10 defers pose estimation
until a module needs it, and `person_down` is attempted on geometry first precisely so that
decision can be made on evidence. Declaring `POSE` as optional-but-unused would be worse than
not declaring it: capability negotiation would report a degradation that changes nothing, which
is how D-008 decays into theatre.

Three terms, each a pure function of the scene:

  * **aspect ratio**, from the current box. A standing adult's box is tall and narrow; a lying
    one is wide.
  * **verticality collapse**, from the track's own trajectory. Height falling to a fraction of
    what *this* track used to be is far more robust than any absolute size, because it needs no
    knowledge of camera geometry or of how far away the subject is.
  * **immobility**, from velocity in box heights per second. A person who collapsed and stayed
    put differs from one who crouched to tie a lace.

The terms are weighted, summed, and mapped into a band that **straddles neutral**. An upright
walking person produces a signal *below* 0.5 — real evidence against the hypothesis — which is
what makes an event release promptly when someone stands back up, rather than waiting for decay.

The module keeps **one integer of state** — the epoch it last saw — and derives everything else
from the scene it is handed, including the standing reference, which comes from the track's own
trajectory. So `observe` is a pure function of the scene in the strict sense, with nothing to
reset and nothing to memoise. An earlier draft cached the last frame's result keyed on frame
identity; the contract suite rightly rejected it, because frame identity does not determine the
whole scene (the capability set can differ), and a cache that assumes it would answer a question
it was not asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.core.capabilities import Capability
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.domain.enums import EventType, ObjectClass, TrackFlag
from vigil.domain.observation import FactValue, Observation, SubjectKey
from vigil.domain.scene import SceneState
from vigil.domain.track import TrackedObject
from vigil.events.builtin.person_down.params import PersonDownParams

MODULE_ID = "person_down"
# A signal this close to neutral says nothing, and a module that says nothing should emit
# nothing — except that an ambiguous posture is not *neutral*, it is an absence of support.
# So ambiguity resolves just below neutral: fail closed (01 §6).
NEUTRAL_EPSILON = 0.01

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.PERSON_DOWN,
    version="1.0.0",
    requires=frozenset({Capability.DETECTION, Capability.TRACKING}),
    optional=frozenset({Capability.VELOCITY}),
    default_params={
        "object_classes": [ObjectClass.PERSON.value],
        "min_track_hits": 4,
        "min_hit_ratio": 0.5,
        "upright_aspect_ratio": 0.55,
        "prone_aspect_ratio": 1.0,
        "collapse_ratio": 0.55,
        "no_collapse_ratio": 0.9,
        "reference_window_frames": 60,
        "min_reference_points": 5,
        "immobile_speed_ratio": 0.15,
        "moving_speed_ratio": 0.6,
        "aspect_weight": 0.4,
        "collapse_weight": 0.4,
        "immobility_weight": 0.2,
        "signal_floor": 0.1,
        "signal_ceiling": 0.9,
        "stillness_bonus_after_ms": 5000.0,
        "stillness_bonus_ratio": 0.05,
    },
    description=(
        "A tracked person whose bounding-box geometry has collapsed from upright to prone and "
        "stayed that way. Geometric only: no pose model is used, it cannot distinguish a fall "
        "from lying down deliberately, and it makes no claim about injury or medical distress."
    ),
)


@dataclass(frozen=True, slots=True)
class _Terms:
    """One subject's geometry, scored. `None` means the term could not be computed."""

    aspect: float
    collapse: float | None
    immobility: float | None
    aspect_ratio: float
    height_ratio: float | None
    speed_ratio: float | None
    reference_height_px: float | None


def _ramp(value: float, low: float, high: float) -> float:
    """0 at or below `low`, 1 at or above `high`, linear between. Never extrapolates."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return min(max((value - low) / (high - low), 0.0), 1.0)


class PersonDownModule:
    """One instance per camera, bound once at startup."""

    __slots__ = ("_epoch", "_params")

    def __init__(self) -> None:
        self._params: PersonDownParams | None = None
        self._epoch: int | None = None

    @property
    def descriptor(self) -> ModuleDescriptor:
        return MODULE

    def bind(self, ctx: ModuleContext) -> None:
        self._params = PersonDownParams.model_validate(dict(ctx.params))
        self._epoch = None

    def release(self) -> None:
        self._epoch = None

    @property
    def params(self) -> PersonDownParams:
        if self._params is None:
            raise RuntimeError("PersonDownModule.observe() called before bind()")
        return self._params

    # ---------------------------------------------------------------- observe

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        meta = scene.frame_meta
        if self._epoch != meta.stream_epoch:
            self._epoch = meta.stream_epoch  # rule 4: bounded state, reset on epoch

        params = self.params
        out: list[Observation] = []
        for track in sorted(scene.tracks, key=lambda t: t.track_id):
            if track.object_class not in params.object_classes:
                continue
            if track.hits < params.min_track_hits or track.hit_ratio < params.min_hit_ratio:
                continue
            if TrackFlag.NEW in track.flags:
                continue
            signal, facts = self._score(scene, track)
            out.append(
                Observation(
                    camera_id=meta.camera_id,
                    module_id=MODULE_ID,
                    subject=SubjectKey("track", str(track.track_id)),
                    signal=signal,
                    t_monotonic_ns=meta.t_monotonic_ns,
                    facts=facts,
                )
            )
        return tuple(out)

    # ---------------------------------------------------------------- scoring

    def _terms(self, scene: SceneState, track: TrackedObject) -> _Terms:
        params = self.params
        box = track.bbox
        height = max(box.height, 1.0)
        aspect_ratio = box.width / height
        aspect = _ramp(aspect_ratio, params.upright_aspect_ratio, params.prone_aspect_ratio)

        reference = self._reference_height(track)
        collapse: float | None = None
        height_ratio: float | None = None
        if reference is not None and reference > 0.0:
            height_ratio = height / reference
            # Inverted ramp: a *small* ratio is the evidence, so 1 at or below collapse_ratio.
            collapse = 1.0 - _ramp(height_ratio, params.collapse_ratio, params.no_collapse_ratio)

        immobility: float | None = None
        speed_ratio: float | None = None
        if Capability.VELOCITY in scene.capabilities:
            speed = track.speed_px_s
            if speed is not None:
                speed_ratio = speed / height
                immobility = 1.0 - _ramp(
                    speed_ratio, params.immobile_speed_ratio, params.moving_speed_ratio
                )
            elif TrackFlag.STATIONARY in track.flags:
                # No fitted velocity yet, but the tracker has already judged it still.
                speed_ratio, immobility = 0.0, 1.0
        return _Terms(
            aspect=aspect,
            collapse=collapse,
            immobility=immobility,
            aspect_ratio=aspect_ratio,
            height_ratio=height_ratio,
            speed_ratio=speed_ratio,
            reference_height_px=reference,
        )

    def _reference_height(self, track: TrackedObject) -> float | None:
        """The tallest this track has measurably been, over its own recent history.

        Measured points only: an interpolated box is the filter's extrapolation, and a reference
        taken from one would compare the subject against a guess about itself.

        `None` when there is too little history to trust — a person first detected while already
        on the ground has no standing reference, and inventing one from two frames would turn
        "we never saw them upright" into "they have not collapsed". The term is then reported
        unavailable and the remaining weights carry the decision.
        """
        params = self.params
        measured = [p for p in track.trajectory.points if not p.is_interpolated]
        recent = measured[-params.reference_window_frames :]
        if len(recent) < params.min_reference_points:
            return None
        return max(p.bbox.height for p in recent)

    def _score(self, scene: SceneState, track: TrackedObject) -> tuple[float, dict[str, FactValue]]:
        """Weighted sum of the available terms, renormalised, mapped into the signal band.

        Renormalising rather than treating a missing term as zero is the whole point: a dropped
        term must not quietly become evidence against. With no velocity, the aspect and collapse
        terms decide, each carrying more weight — which is exactly what "running DEGRADED" should
        mean.
        """
        params = self.params
        terms = self._terms(scene, track)
        weighted: list[tuple[str, float, float]] = [("aspect", terms.aspect, params.aspect_weight)]
        if terms.collapse is not None:
            weighted.append(("collapse", terms.collapse, params.collapse_weight))
        if terms.immobility is not None:
            weighted.append(("immobility", terms.immobility, params.immobility_weight))

        total_weight = sum(weight for _name, _value, weight in weighted)
        score = (
            sum(value * weight for _name, value, weight in weighted) / total_weight
            if total_weight > 0.0
            else 0.0
        )

        facts: dict[str, FactValue] = {
            "track_id": float(track.track_id),
            "object_class": track.object_class.value,
            "hits": float(track.hits),
            "hit_ratio": round(track.hit_ratio, 4),
            "aspect_ratio": round(terms.aspect_ratio, 4),
            "aspect_term": round(terms.aspect, 4),
            "method": "bounding-box geometry; no pose model",
        }
        if terms.collapse is not None and terms.height_ratio is not None:
            facts["height_ratio"] = round(terms.height_ratio, 4)
            facts["collapse_term"] = round(terms.collapse, 4)
            if terms.reference_height_px is not None:
                facts["reference_height_px"] = round(terms.reference_height_px, 1)
        else:
            # Named, so an operator reading the explanation knows which evidence was absent
            # rather than assuming it was weighed and found wanting.
            facts["collapse_unavailable"] = "too little measured history for a standing reference"
        if terms.immobility is not None and terms.speed_ratio is not None:
            facts["speed_box_heights_s"] = round(terms.speed_ratio, 4)
            facts["immobility_term"] = round(terms.immobility, 4)
        else:
            facts["velocity_unavailable"] = True

        bonus = 0.0
        stationary_ms = track.stationary_duration_ms
        if stationary_ms is not None:
            facts["stationary_duration_ms"] = round(stationary_ms, 1)
            if stationary_ms >= params.stillness_bonus_after_ms:
                bonus = params.stillness_bonus_ratio
                facts["stillness_bonus"] = bonus

        span = params.signal_ceiling - params.signal_floor
        signal = params.signal_floor + span * min(score + bonus, 1.0)
        signal = min(max(signal, params.signal_floor), params.signal_ceiling)
        if abs(signal - 0.5) < NEUTRAL_EPSILON:
            signal = 0.5 - NEUTRAL_EPSILON
            facts["ambiguous"] = True
        facts["score"] = round(score, 4)
        facts["signal"] = round(signal, 4)
        return (signal, facts)


def create() -> PersonDownModule:
    """The factory the registry discovers (`events/registry.py`)."""
    return PersonDownModule()
