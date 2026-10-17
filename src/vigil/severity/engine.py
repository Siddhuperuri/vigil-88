"""Scoring a verified event (04 §1 `score`, 06 §8).

    severity_score = 100 * clamp( sum_i ( weight_i * factor_i ), 0, 1 )

Three properties this holds to:

  1. **Weights sum to 1.0**, validated at startup by `SeverityWeights`. A config whose weights
     sum to 1.3 would silently saturate every event at CRITICAL.
  2. **Severity never feeds back into confidence.** They answer different questions, and coupling
     them would make a dangerous event look more certain merely because it is dangerous.
  3. **Explainable as arithmetic.** Every factor's raw value, weight and contribution is recorded
     and they sum to the score, so an operator can see which factor to argue with.

The UI does not decide severity, and a module cannot bypass this engine: the pipeline scores
every verified event here, and nothing else constructs a `SeverityAssessment`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from vigil.config.schema.severity import SeverityConfig
from vigil.core.protocols.logger import LoggerLike
from vigil.domain.enums import Severity
from vigil.domain.incident import MAX_SEVERITY_SCORE, SeverityAssessment, SeverityFactor
from vigil.severity.factors import (
    FactorInputs,
    factor_base,
    factor_camera_trust,
    factor_confidence,
    factor_escalation,
    factor_object_count,
    factor_persistence,
    factor_time_of_day,
    factor_zone_criticality,
)

FactorFn = Callable[[FactorInputs, SeverityConfig], float]

# Order is the order the explanation renders in, and matches the table in 06 §8.
FACTOR_ORDER: tuple[tuple[str, FactorFn], ...] = (
    ("base", factor_base),
    ("zone_criticality", factor_zone_criticality),
    ("confidence", factor_confidence),
    ("object_count", factor_object_count),
    ("persistence", factor_persistence),
    ("time_of_day", factor_time_of_day),
    ("escalation", factor_escalation),
    ("camera_trust", factor_camera_trust),
)


def band_for(score: float, config: SeverityConfig) -> Severity:
    """Map a score to a band. Both are persisted: the band drives the UI and alert routing, the
    number drives sorting within a band."""
    bands = config.bands
    if score >= bands.CRITICAL:
        return Severity.CRITICAL
    if score >= bands.HIGH:
        return Severity.HIGH
    if score >= bands.MODERATE:
        return Severity.MODERATE
    if score >= bands.LOW:
        return Severity.LOW
    return Severity.INFO


class SeverityScorer:
    """Stateless given its config, so it is safe to share and trivial to replay."""

    __slots__ = ("_config", "_log")

    def __init__(self, *, config: SeverityConfig, logger: LoggerLike) -> None:
        self._config = config
        self._log = logger.bind(stage="score")

    @property
    def config(self) -> SeverityConfig:
        return self._config

    def score(self, inputs: FactorInputs) -> SeverityAssessment:
        weights: Mapping[str, float] = self._config.weights.model_dump()
        factors: list[SeverityFactor] = []
        degraded = False
        total = 0.0

        for name, function in FACTOR_ORDER:
            weight = float(weights[name])
            try:
                raw = function(inputs, self._config)
            except Exception as exc:  # noqa: BLE001 - fall back, never fail to score
                # 01 §6: severity fails *open* — an event we cannot score fully is still worth
                # showing. The fallback is the event type's intrinsic seriousness, and the
                # assessment is marked degraded so nothing presents it as a full score.
                degraded = True
                raw = 0.0
                self._log.warning(
                    "severity_factor_failed",
                    factor=name,
                    error=f"{type(exc).__name__}: {exc}",
                )
            contribution = weight * raw
            total += contribution
            factors.append(
                SeverityFactor(
                    name=name,
                    raw_value=raw,
                    weight=weight,
                    contribution=contribution,
                )
            )

        score = MAX_SEVERITY_SCORE * min(max(total, 0.0), 1.0)
        return SeverityAssessment(
            score=score,
            severity=band_for(score, self._config),
            factors=tuple(factors),
            degraded=degraded,
        )

    def explain(self, assessment: SeverityAssessment) -> str:
        """One line an operator can read, summing to the score."""
        parts = " + ".join(
            f"{f.name} {f.raw_value:.2f}x{f.weight:.2f}={f.contribution:.3f}"
            for f in assessment.factors
            if f.weight > 0.0
        )
        return f"{parts} -> {assessment.score:.1f} ({assessment.severity.name})"


def thresholds_in_force(config: SeverityConfig) -> dict[str, float]:
    """The threshold values to capture on the event, so an explanation rendered later reflects
    the thresholds the decision was actually made under (05 §4)."""
    bands = config.bands
    thresholds: dict[str, float] = {
        "severity.bands.LOW": bands.LOW,
        "severity.bands.MODERATE": bands.MODERATE,
        "severity.bands.HIGH": bands.HIGH,
        "severity.bands.CRITICAL": bands.CRITICAL,
        "severity.count_saturation": config.count_saturation,
        "severity.persistence_saturation_ms": config.persistence_saturation_ms,
        "severity.escalation_saturation": config.escalation_saturation,
    }
    for name, weight in config.weights.model_dump().items():
        thresholds[f"severity.weights.{name}"] = float(weight)
    return thresholds
