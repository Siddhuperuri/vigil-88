"""Building the explanation that travels with an incident (01 §7.5, 05 §4).

A monitoring system whose decisions an operator cannot interrogate gets switched off within a
week, so this is a reliability feature rather than a nicety.

Two rules shape the whole file:

  * **It is built once, as the decision is made, and never recomputed.** Thresholds are copied
     in rather than read from config at display time: thresholds change as a system is tuned,
     and an explanation rendered against today's values misrepresents a decision made under
     yesterday's.
  * **It is structured data, not log text.** Log files are not queryable from the console
     (01 §7.2), and "why did this fire" has to be answerable months later from the record alone.

The trigger summary is one human sentence. It is assembled from facts the module already
reported, never from a template that could outrun what was actually observed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vigil.config.settings import Settings
from vigil.domain.event import AccumulatorSample, VerifiedEvent
from vigil.domain.incident import (
    IncidentExplanation,
    ObservationSummary,
    SeverityAssessment,
)
from vigil.domain.observation import FactValue, Observation


# The thresholds that actually governed the decision. Deliberately a short, named list rather
# than the whole config: an explanation is meant to be read.
def threshold_snapshot(settings: Settings) -> dict[str, float]:
    temporal, verification = settings.temporal, settings.verification
    return {
        "temporal.half_life_ms": float(temporal.half_life_ms),
        "temporal.l_activate": float(temporal.l_activate),
        "temporal.l_release": float(temporal.l_release),
        "temporal.l_bound": float(temporal.l_bound),
        "temporal.min_observations": float(temporal.min_observations),
        "temporal.min_support_ratio": float(temporal.min_support_ratio),
        "temporal.min_duration_ms": float(temporal.min_duration_ms),
        "temporal.release_sustain_ms": float(temporal.release_sustain_ms),
        "verification.min_track_hit_ratio": float(verification.min_track_hit_ratio),
        "verification.min_stability_ratio": float(verification.min_stability_ratio),
        "verification.max_frame_age_ms": float(verification.max_frame_age_ms),
        "verification.cooldown_ms": float(verification.cooldown_ms),
        "verification.max_events_per_window": float(verification.max_events_per_window),
    }


def summarise_observations(
    observations: Sequence[Observation], *, limit: int
) -> tuple[ObservationSummary, ...]:
    """Keep the most recent `limit` observations, oldest first.

    Bounded because a long-running situation can accumulate hundreds and the explanation is
    stored on the incident; the recent ones are the ones that caused the promotion.
    """
    kept = sorted(observations, key=lambda o: o.t_monotonic_ns)[-limit:]
    return tuple(
        ObservationSummary(t_monotonic_ns=o.t_monotonic_ns, signal=o.signal, facts=dict(o.facts))
        for o in kept
    )


def _fact_text(facts: Mapping[str, FactValue], key: str) -> str | None:
    value = facts.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return key if value else None
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def trigger_summary(event: VerifiedEvent, assessment: SeverityAssessment) -> str:
    """One sentence an operator reads first. Built from observed facts, never invented.

    It says what was observed and how strongly, and nothing about what it means: the module
    reported support for a hypothesis, and this sentence must not promote that into a claim
    (D-003).
    """
    candidate = event.candidate
    observations = candidate.observations
    facts: Mapping[str, FactValue] = observations[-1].facts if observations else {}
    subject = str(candidate.subject)
    where = _fact_text(facts, "zone_id")
    what = _fact_text(facts, "object_class")
    parts = [f"{candidate.module_id} reported {candidate.event_type.value}"]
    if what:
        parts.append(f"for a {what}")
    parts.append(f"({subject})")
    if where:
        parts.append(f"in zone {where}")
    span_ms = candidate.accumulator.last_observation_ns - candidate.accumulator.first_observation_ns
    parts.append(f"over {len(observations)} observation(s) spanning {span_ms / 1_000_000:.0f} ms")
    parts.append(
        f"-- confidence {event.confidence_ratio:.2f}, severity "
        f"{assessment.severity.name} ({assessment.score:.1f})"
    )
    return " ".join(parts)


def build_explanation(
    *,
    event: VerifiedEvent,
    assessment: SeverityAssessment,
    settings: Settings,
    accumulator_trace: Sequence[AccumulatorSample] = (),
    caveats: Sequence[str] = (),
    max_observations: int = 64,
) -> IncidentExplanation:
    """Assemble the complete record of one decision.

    `accumulator_trace` is passed in rather than recomputed: it is the accumulator's actual
    trajectory at the moment of promotion, and nothing can reconstruct it afterwards.
    """
    return IncidentExplanation(
        trigger_summary=trigger_summary(event, assessment),
        observations=summarise_observations(event.candidate.observations, limit=max_observations),
        accumulator_trace=tuple(accumulator_trace),
        # Every verdict, including the passes: without them you cannot tell a validator that
        # passed from one that never ran (06 §6).
        validator_verdicts=tuple(event.verdicts),
        severity_factors=tuple(assessment.factors),
        thresholds=threshold_snapshot(settings),
        capability_caveats=tuple(caveats),
    )
