"""The degradation ladder (01 §9): what the engine gives up, in order, when it cannot keep up.

The thresholds are **starting points, not measurements**. They were chosen so a healthy machine
never trips them and a clearly overloaded one does; they have not been tuned against a measured
overload run on the target hardware. `vigil bench` and a soak are how to tune them, and the
ladder reports its reason every time it moves so a bad threshold is visible rather than silent.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel


class DegradationConfig(ConfigModel):
    enabled: bool = True
    # How often pressure is sampled. Escalation and recovery are counted in samples of this size.
    interval_ms: int = Field(default=1000, ge=100, le=60_000)
    # Pressure means analysis dropped at least this many frames within one interval ...
    drop_threshold: int = Field(default=5, ge=1)
    # ... or the detector's recent p95 latency is above this ceiling.
    latency_ceiling_ms: float = Field(default=1500.0, gt=0)
    # Pressure must last this long before a step down, and calm must last this long before a step
    # back up. Recovery is slower on purpose: a ladder that flaps is worse than one that is slow.
    escalate_after_ms: int = Field(default=10_000, ge=1000)
    recover_after_ms: int = Field(default=60_000, ge=1000)
    # Step 1 and step 2 scale every camera's inference rate by these factors.
    reduced_rate_scale: float = Field(default=0.5, gt=0, lt=1)
    priority_only_rate_scale: float = Field(default=0.25, gt=0, lt=1)

    @model_validator(mode="after")
    def _recovery_not_faster_than_escalation(self) -> Self:
        if self.recover_after_ms < self.escalate_after_ms:
            raise ValueError(
                "degradation.recover_after_ms must not be shorter than escalate_after_ms"
            )
        if self.priority_only_rate_scale > self.reduced_rate_scale:
            raise ValueError("priority_only_rate_scale must not exceed reduced_rate_scale")
        return self
