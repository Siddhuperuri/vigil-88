"""The abandoned_object module: a stationary object whose owner has left (06 §11).

The distinction this module exists to make is between **stationary** and **abandoned**, and it
is not a small one. A bin is stationary. A parked bicycle is stationary. A potted plant has been
stationary for years. None of them is abandoned, and a module that fired on "stationary in one
frame" would fire on all of them, forever, on every camera — which is the fastest possible way
to teach an operator to ignore this system.

So the hypothesis has three parts, and all three have to hold:

  1. **It is an object of a watched class** that the tracker has judged `STATIONARY` — not
     merely slow, not merely small, but still for `tracking.stationary_min_duration_ms`.
  2. **No plausible owner is near it now**, where "near" is measured in the object's own box
     heights so the threshold means the same thing near and far.
  3. **An owner was near it, and then left.** This is the part that separates a dropped bag from
     the furniture, and it is why the module keeps state: one integer per live object, the last
     time a person was beside it.

That third condition is the sequence `object present -> owner nearby -> owner gone -> object
persists`, which 06 §11 names as what this module exists to exercise. It is expressed as
accumulating evidence rather than as a `SequenceMatcher` pattern on purpose: a matcher would have
to be fed ordered marks and would reset on the first frame the tracker blinked, whereas "the last
time an owner was seen nearby" survives a detection gap and degrades gracefully. The matcher in
`temporal/sequence.py` remains the right tool for patterns whose *steps* are discrete events;
here the steps are continuous states.

**Furniture is handled honestly rather than cleverly.** An object that has been stationary since
before the camera started has no observed owner, so condition 3 never holds and the module stays
silent about it — permanently, with no list of known objects to maintain and no learning. The
cost is the matching real failure: a bag dropped before the camera came online is invisible to
this module too. Both are stated in the README.

State is bounded and pruned every frame to the tracks actually present, and every update is
idempotent (it writes an absolute timestamp), so observing one frame twice says the same thing
twice without a cache.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vigil.core.capabilities import Capability
from vigil.core.protocols.module import ModuleContext, ModuleDescriptor
from vigil.core.units import ns_to_ms
from vigil.domain.enums import EventType, ObjectClass, TrackFlag, ZoneKind
from vigil.domain.observation import FactValue, Observation, SubjectKey
from vigil.domain.scene import SceneState
from vigil.domain.track import TrackedObject
from vigil.events.builtin.abandoned_object.params import AbandonedObjectParams

MODULE_ID = "abandoned_object"

MODULE = ModuleDescriptor(
    module_id=MODULE_ID,
    event_type=EventType.ABANDONED_OBJECT,
    version="1.0.0",
    requires=frozenset({Capability.DETECTION, Capability.TRACKING}),
    optional=frozenset({Capability.ZONES}),
    default_params={
        "object_classes": [ObjectClass.OBJECT_BAG.value, ObjectClass.OBJECT_BOX.value],
        "owner_classes": [ObjectClass.PERSON.value],
        "zone_kinds": [
            ZoneKind.RESTRICTED.value,
            ZoneKind.REGION_OF_INTEREST.value,
            ZoneKind.COUNTING.value,
        ],
        "min_track_hits": 6,
        "min_hit_ratio": 0.5,
        "owner_distance_ratio": 2.5,
        "separation_after_ms": 10_000.0,
        "persistence_after_ms": 30_000.0,
        "base_signal": 0.6,
        "separation_bonus_ratio": 0.15,
        "persistence_bonus_ratio": 0.15,
        "attended_signal": 0.15,
        "max_signal": 0.95,
        "max_objects": 32,
    },
    description=(
        "A stationary object of a watched class that a person was seen beside and then left. "
        "It reports a persisting state, not an act: it cannot tell a forgotten bag from a "
        "deliberately placed one, and it stays silent about objects it never saw an owner for."
    ),
)


@dataclass(frozen=True, slots=True)
class _Owner:
    """The nearest plausible owner right now, if any."""

    track_id: int | None
    distance_ratio: float | None


class AbandonedObjectModule:
    """One instance per camera, bound once at startup."""

    __slots__ = ("_epoch", "_owner_seen_ns", "_params")

    def __init__(self) -> None:
        self._params: AbandonedObjectParams | None = None
        self._epoch: int | None = None
        # track_id -> the last monotonic instant a plausible owner was beside it. Pruned to the
        # live track set every frame, so this is bounded by the number of visible objects.
        self._owner_seen_ns: dict[int, int] = {}

    @property
    def descriptor(self) -> ModuleDescriptor:
        return MODULE

    def bind(self, ctx: ModuleContext) -> None:
        self._params = AbandonedObjectParams.model_validate(dict(ctx.params))
        self._epoch = None
        self._owner_seen_ns = {}

    def release(self) -> None:
        self._epoch = None
        self._owner_seen_ns = {}

    @property
    def params(self) -> AbandonedObjectParams:
        if self._params is None:
            raise RuntimeError("AbandonedObjectModule.observe() called before bind()")
        return self._params

    # ---------------------------------------------------------------- observe

    def observe(self, scene: SceneState) -> Sequence[Observation]:
        params = self.params
        meta = scene.frame_meta
        if self._epoch != meta.stream_epoch:
            # Rule 4: identities no longer mean what they meant, so ownership history keyed by
            # them would attribute one object's owner to another.
            self._epoch = meta.stream_epoch
            self._owner_seen_ns = {}

        objects = self._watched(scene)
        owners = [t for t in scene.tracks if t.object_class in params.owner_classes]
        self._prune(scene)

        out: list[Observation] = []
        for track in objects:
            owner = self._nearest_owner(track, owners)
            if owner.track_id is not None:
                # Idempotent: re-observing the same frame writes the same instant.
                self._owner_seen_ns[track.track_id] = meta.t_monotonic_ns
            signal, facts = self._score(scene, track, owner, now_ns=meta.t_monotonic_ns)
            if signal is None:
                continue
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

    def _prune(self, scene: SceneState) -> None:
        """Forget objects that are no longer tracked. Without this, ownership history grows by
        one entry per object the camera has ever seen (04 §3.5 rule 4)."""
        live = {t.track_id for t in scene.tracks}
        for track_id in [tid for tid in self._owner_seen_ns if tid not in live]:
            del self._owner_seen_ns[track_id]

    def _watched(self, scene: SceneState) -> list[TrackedObject]:
        """Objects worth reasoning about, in a deterministic order and bounded in number.

        With `ZONES` granted the module watches only the configured zone kinds: a bag left on
        the pavement outside the fence is not this camera's problem, and saying so in config is
        better than saying it in a threshold. Without `ZONES` it watches the whole frame, which
        is the honest fallback — the camera has no notion of where it cares about.
        """
        params = self.params
        candidates = [
            t
            for t in scene.tracks
            if t.object_class in params.object_classes
            and t.hits >= params.min_track_hits
            and t.hit_ratio >= params.min_hit_ratio
            and TrackFlag.NEW not in t.flags
        ]
        if Capability.ZONES in scene.capabilities:
            watched_ids = {z.zone_id for z in scene.zones if z.kind in params.zone_kinds}
            inside = {
                track_id
                for zone_id, occupancy in scene.occupancy.items()
                if zone_id in watched_ids
                for track_id in occupancy.track_ids
            }
            candidates = [t for t in candidates if t.track_id in inside]
        candidates.sort(key=lambda t: t.track_id)
        return candidates[: params.max_objects]

    def _nearest_owner(self, track: TrackedObject, owners: Sequence[TrackedObject]) -> _Owner:
        """The closest plausible owner, in object-box heights. Only visible owners count: a
        predicted box is the filter's guess, and "someone might be standing there" is not
        evidence that the bag is attended."""
        params = self.params
        height = max(track.bbox.height, 1.0)
        best: _Owner = _Owner(None, None)
        for owner in owners:
            if not owner.is_visible or TrackFlag.NEW in owner.flags:
                continue
            ratio = track.bbox.gap_to(owner.bbox) / height
            if ratio > params.owner_distance_ratio:
                continue
            if best.distance_ratio is None or ratio < best.distance_ratio:
                best = _Owner(owner.track_id, ratio)
        return best

    # ---------------------------------------------------------------- scoring

    def _score(
        self,
        scene: SceneState,
        track: TrackedObject,
        owner: _Owner,
        *,
        now_ns: int,
    ) -> tuple[float | None, dict[str, FactValue]]:
        """The signal, or `None` when there is nothing to say about this object.

        Silence is the right answer more often than a weak signal is. An object nobody has ever
        been seen beside is furniture as far as this module can tell, and emitting a steady
        trickle of weak evidence about the bins would eventually confirm one.
        """
        params = self.params
        facts: dict[str, FactValue] = {
            "track_id": float(track.track_id),
            "object_class": track.object_class.value,
            "hits": float(track.hits),
            "hit_ratio": round(track.hit_ratio, 4),
        }
        zone_id = self._zone_of(scene, track)
        if zone_id is not None:
            facts["zone_id"] = zone_id
        elif Capability.ZONES not in scene.capabilities:
            # Without ZONES the module watches the whole frame and cannot say where the object
            # is. Naming the gap beats omitting it: an explanation with no location should say
            # why it has none.
            facts["zones_unavailable"] = True

        stationary = TrackFlag.STATIONARY in track.flags
        stationary_ms = track.stationary_duration_ms
        facts["stationary"] = stationary
        if stationary_ms is not None:
            facts["stationary_duration_ms"] = round(stationary_ms, 1)

        if owner.track_id is not None and owner.distance_ratio is not None:
            # Attended. Evidence against, so a returning owner releases the event rather than
            # waiting for decay.
            facts["owner_track_id"] = float(owner.track_id)
            facts["owner_distance_ratio"] = round(owner.distance_ratio, 3)
            facts["attended"] = True
            facts["signal"] = params.attended_signal
            return (params.attended_signal, facts)

        if not stationary:
            # Moving, or not yet judged still. An object in motion is being carried.
            return (None, facts)

        seen_ns = self._owner_seen_ns.get(track.track_id)
        if seen_ns is None:
            # Never observed with an owner: furniture, or an object that predates this track.
            facts["owner_never_observed"] = True
            return (None, facts)

        separation_ms = ns_to_ms(now_ns - seen_ns)
        facts["separation_ms"] = round(separation_ms, 1)
        if separation_ms < params.separation_after_ms:
            # The owner has only just stepped away. Setting a bag down and standing up is not
            # abandoning it, and this is the window that distinguishes the two.
            facts["owner_recently_present"] = True
            return (None, facts)

        signal = params.base_signal
        facts["base_signal"] = params.base_signal
        signal += params.separation_bonus_ratio
        facts["separation_bonus"] = params.separation_bonus_ratio
        if stationary_ms is not None and stationary_ms >= params.persistence_after_ms:
            signal += params.persistence_bonus_ratio
            facts["persistence_bonus"] = params.persistence_bonus_ratio

        clamped = min(signal, params.max_signal)
        facts["signal"] = round(clamped, 4)
        return (clamped, facts)

    @staticmethod
    def _zone_of(scene: SceneState, track: TrackedObject) -> str | None:
        """Which watched zone the object is in, or None.

        Gated on the capability rather than on whether `scene.zones` happens to be populated: a
        module must report what the running configuration actually supplies (D-008), and
        occupancy computed without the ZONES capability granted is not something to quote.
        """
        if Capability.ZONES not in scene.capabilities:
            return None
        for zone_id in sorted(scene.occupancy):
            if track.track_id in scene.occupancy[zone_id].track_ids:
                return zone_id
        return None


def create() -> AbandonedObjectModule:
    """The factory the registry discovers (`events/registry.py`)."""
    return AbandonedObjectModule()
