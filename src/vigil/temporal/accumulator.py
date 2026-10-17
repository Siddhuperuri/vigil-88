"""Log-odds accumulation with half-life decay and dual-threshold hysteresis (D-007, 04 §6).

The update rule in `observe` is the specification transcribed, in the specification's order:

    1.  L <- L * 2 ** (-dt_ms / half_life_ms)            decay toward zero
    2.  s <- clamp(signal, floor, 1 - floor)             no single frame may dominate
    3.  L <- L + module_weight * ln(s / (1 - s))         add this frame's evidence
    4.  L <- clamp(L, -l_bound, +l_bound)                bounded recovery time

Four properties follow, and each is a property test (04 §9):

  * `signal = 0.5` contributes exactly zero, because `ln(0.5/0.5) = 0`. A module that is
    persistently unsure can never confirm an event by being unsure for long enough.
  * stronger evidence never lowers `L`, because `ln(s/(1-s))` rises monotonically in `s`.
  * evidence fades smoothly; nothing appears or vanishes because a window edge moved.
  * `L` stays inside `[-l_bound, +l_bound]`, so recovery from saturation is bounded.

No clock is read here. Time arrives on the observation, which got it from the frame, which got
it from an injected `Clock` (01 §7.6). That is what makes a ten-minute scenario a two-
millisecond test and makes replay behave identically to live capture.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from vigil.config.schema.modules import TemporalConfig
from vigil.core.errors import TemporalError
from vigil.core.units import NS_PER_MS, ns_to_ms
from vigil.domain.event import AccumulatorSample, AccumulatorState
from vigil.domain.observation import Observation, SubjectKey

# Above this, the logistic is 1.0 in float64 anyway; computing exp(-x) on it would overflow.
_LOGISTIC_SATURATION = 700.0
# A signal is "supporting" when it carries positive evidence, i.e. it exceeds pure ambiguity.
NEUTRAL_SIGNAL = 0.5


def log_odds_of(signal: float, *, signal_floor: float) -> float:
    """Step 2 and the `ln` of step 3: this frame's evidence, in log-odds.

    Clamping away from 0 and 1 is what bounds one frame's contribution: without it a detector
    reporting 1.0 would add infinite evidence and no later doubt could ever undo it.
    """
    s = min(max(signal, signal_floor), 1.0 - signal_floor)
    return math.log(s / (1.0 - s))


def confidence_of(log_odds: float, *, confidence_scale: float) -> float:
    """The logistic mapping of 04 §6.2, for display and thresholds.

    This is calibrated-*looking* and is not a calibrated probability. The UI labels it
    "confidence" and never "probability": claiming calibration without a reliability study
    would be a lie with a decimal point on it.
    """
    x = log_odds / confidence_scale
    if x >= _LOGISTIC_SATURATION:
        return 1.0
    if x <= -_LOGISTIC_SATURATION:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def decayed(log_odds: float, *, dt_ms: float, half_life_ms: float) -> float:
    """Step 1. Exactly halves `log_odds` over `half_life_ms`; a zero step changes nothing."""
    if dt_ms <= 0.0:
        return log_odds
    return log_odds * math.pow(2.0, -dt_ms / half_life_ms)


@dataclass(frozen=True, slots=True)
class _Entry:
    """One observation and the belief it produced.

    Retaining the whole observation rather than just its signal is what lets a candidate carry
    the window that promoted it, with every `facts` mapping intact: those facts are the
    explanation an operator reads and the fixture a regression test asserts against (06 §4).
    """

    observation: Observation
    log_odds_after: float

    @property
    def t_monotonic_ns(self) -> int:
        return self.observation.t_monotonic_ns

    @property
    def signal(self) -> float:
        return self.observation.signal


class Accumulator:
    """Belief about one hypothesis: one `(camera_id, module_id, subject)` (04 §6.1).

    Owned by exactly one thread, the camera's analysis thread (D-005), so it holds no lock.
    """

    __slots__ = (
        "_active",
        "_camera_id",
        "_config",
        "_first_ns",
        "_last_ns",
        "_log_odds",
        "_module_id",
        "_release_since_ns",
        "_samples",
        "_slot_ms",
        "_subject",
        "_t_ns",
        "_total_observations",
        "_weight",
    )

    def __init__(
        self,
        *,
        camera_id: str,
        module_id: str,
        subject: SubjectKey,
        config: TemporalConfig,
        support_slot_ms: float,
    ) -> None:
        if support_slot_ms <= 0.0:
            raise TemporalError(
                "support_slot_ms must be positive: it is the expected spacing between "
                "observations and divides the support window",
                context={"support_slot_ms": support_slot_ms},
            )
        self._camera_id = camera_id
        self._module_id = module_id
        self._subject = subject
        self._config = config
        self._slot_ms = support_slot_ms
        self._weight = config.weight_for(module_id)
        self._log_odds = 0.0  # even odds: no evidence either way
        self._samples: deque[_Entry] = deque(maxlen=config.max_window_observations)
        self._first_ns: int | None = None
        # Two different instants, and conflating them over-decays: `_t_ns` is the time `_log_odds`
        # is current as of (advanced by every decay), `_last_ns` is when evidence last arrived
        # (advanced only by an observation, and what idle expiry measures).
        self._t_ns: int | None = None
        self._last_ns: int | None = None
        self._total_observations = 0
        self._active = False
        self._release_since_ns: int | None = None

    # ---------------------------------------------------------------- identity

    @property
    def key(self) -> tuple[str, str, SubjectKey]:
        return (self._camera_id, self._module_id, self._subject)

    @property
    def log_odds(self) -> float:
        return self._log_odds

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def last_observation_ns(self) -> int | None:
        """When evidence last arrived. What idle expiry measures."""
        return self._last_ns

    @property
    def current_ns(self) -> int | None:
        """The instant `log_odds` is current as of. Decay may have advanced it past the last
        observation, and it must never run backwards."""
        return self._t_ns

    @property
    def first_observation_ns(self) -> int | None:
        return self._first_ns

    def window(self) -> tuple[Observation, ...]:
        """The retained observations, oldest first. This is the window a candidate carries."""
        return tuple(e.observation for e in self._samples)

    def trace(self) -> tuple[AccumulatorSample, ...]:
        """How belief evolved, one point per retained observation (01 §7.5).

        Captured as it happened. An explanation recomputed later against today's config would
        misrepresent a decision made under yesterday's.
        """
        return tuple(
            AccumulatorSample(
                t_monotonic_ns=e.t_monotonic_ns,
                log_odds=e.log_odds_after,
                confidence_ratio=confidence_of(
                    e.log_odds_after, confidence_scale=self._config.confidence_scale
                ),
            )
            for e in self._samples
        )

    # ---------------------------------------------------------------- the update rule

    def observe(self, obs: Observation) -> AccumulatorState:
        """Apply one observation. Rejects anything that does not belong to this accumulator."""
        if (obs.camera_id, obs.module_id, obs.subject) != self.key:
            raise TemporalError(
                "observation does not belong to this accumulator",
                context={"expected": str(self._subject), "got": str(obs.subject)},
            )
        if self._t_ns is not None and obs.t_monotonic_ns < self._t_ns:
            raise TemporalError(
                "observation timestamp precedes the last update: time cannot run backwards "
                "within a stream epoch",
                context={"last_ns": self._t_ns, "got_ns": obs.t_monotonic_ns},
            )
        self._decay_to(obs.t_monotonic_ns)
        self._log_odds = self._bounded(
            self._log_odds
            + self._weight * log_odds_of(obs.signal, signal_floor=self._config.signal_floor)
        )
        if self._first_ns is None:
            self._first_ns = obs.t_monotonic_ns
        self._last_ns = self._t_ns = obs.t_monotonic_ns
        self._total_observations += 1
        self._samples.append(_Entry(obs, self._log_odds))
        self._update_release(obs.t_monotonic_ns)
        return self.state

    def decay_to(self, t_monotonic_ns: int) -> AccumulatorState:
        """Advance time without new evidence. This is how absence of evidence is evidence."""
        self._decay_to(t_monotonic_ns)
        self._update_release(t_monotonic_ns)
        return self.state_at(t_monotonic_ns)

    def _decay_to(self, t_monotonic_ns: int) -> None:
        if self._t_ns is None:
            return
        dt_ms = ns_to_ms(t_monotonic_ns - self._t_ns)
        if dt_ms < 0.0:
            raise TemporalError(
                "cannot decay to a time before the last update",
                context={"last_ns": self._t_ns, "to_ns": t_monotonic_ns},
            )
        before = self._log_odds
        self._log_odds = decayed(before, dt_ms=dt_ms, half_life_ms=self._config.half_life_ms)
        self._note_release_crossing(before, from_ns=self._t_ns, to_ns=t_monotonic_ns)
        self._t_ns = t_monotonic_ns

    def _note_release_crossing(self, before: float, *, from_ns: int, to_ns: int) -> None:
        """Date the drop below `l_release` to when it actually happened, not to this tick.

        Decay is deterministic and invertible, so the crossing instant is exact arithmetic. Using
        the tick time instead would make the release depend on how often the engine happens to
        look: a camera analysed at 2 fps would take a second longer to resolve than one at 10 fps,
        and a single long step could never satisfy `release_sustain_ms` at all.
        """
        release = self._config.l_release
        if not self._active or before <= release or self._log_odds > release:
            return
        if release <= 0.0 or before <= 0.0:
            self._release_since_ns = to_ns
            return
        dt_ms = self._config.half_life_ms * math.log2(before / release)
        crossed_ns = from_ns + int(dt_ms * NS_PER_MS)
        self._release_since_ns = min(max(crossed_ns, from_ns), to_ns)

    def _bounded(self, value: float) -> float:
        bound = self._config.l_bound
        return min(max(value, -bound), bound)

    # ---------------------------------------------------------------- hysteresis (04 §6.3)

    def _update_release(self, now_ns: int) -> None:
        """Track how long an active accumulator has sat at or below `l_release`."""
        if not self._active:
            self._release_since_ns = None
            return
        if self._log_odds <= self._config.l_release:
            if self._release_since_ns is None:
                self._release_since_ns = now_ns
        else:
            self._release_since_ns = None

    def promotion_ready(self, now_ns: int) -> bool:
        """All four promotion conditions of 04 §6.3. Already-active accumulators do not
        re-promote: the situation they describe is the one already being reported.

        `min_duration_ms` is measured across the **evidence span** — first observation to last —
        and not from the first observation to now. The difference decides whether a brief
        accidental entry can confirm. A person inside a zone for 900 ms has produced 900 ms of
        evidence; if the elapsed time were measured to now, that burst would still satisfy a
        1200 ms threshold a few frames after they left, and `min_duration_ms` would be
        satisfiable by waiting rather than by the situation persisting. 06 §14 requires the
        opposite: a brief crossing must produce no candidate at all.
        """
        if self._active or self._first_ns is None or self._last_ns is None:
            return False
        cfg = self._config
        if self._log_odds < cfg.l_activate:
            return False
        if self._total_observations < cfg.min_observations:
            return False
        if ns_to_ms(self._last_ns - self._first_ns) < cfg.min_duration_ms:
            return False
        return self.support_ratio(now_ns) >= cfg.min_support_ratio

    @property
    def evidence_span_ms(self) -> float:
        """How long evidence actually arrived for, which is what `min_duration_ms` tests."""
        if self._first_ns is None or self._last_ns is None:
            return 0.0
        return ns_to_ms(self._last_ns - self._first_ns)

    def mark_active(self, now_ns: int) -> None:
        if self._active:
            raise TemporalError(
                "accumulator is already active", context={"key": str(self._subject)}
            )
        self._active = True
        self._update_release(now_ns)

    def release_ready(self, now_ns: int) -> bool:
        """`L <= l_release` sustained for `release_sustain_ms`. The gap below `l_activate` is
        the hysteresis that stops an event flapping while someone stands on a boundary."""
        if not self._active or self._release_since_ns is None:
            return False
        return ns_to_ms(now_ns - self._release_since_ns) >= self._config.release_sustain_ms

    def mark_released(self) -> None:
        """End the episode. The accumulator survives; the window it accumulated does not.

        A released event is over, and a recurrence is a new event with its own duration and its
        own evidence (06 §9). Keeping the old window would let a situation that ended hours ago
        satisfy `min_duration_ms` for the next one instantly. The decayed `L` is kept, because
        it is a true statement about evidence that genuinely has not fully faded yet.
        """
        self._active = False
        self._release_since_ns = None
        self._samples.clear()
        self._first_ns = None
        self._total_observations = 0

    def is_idle_at(self, now_ns: int) -> bool:
        """True when this accumulator may be discarded: inactive and long unobserved."""
        if self._active or self._last_ns is None:
            return False
        return ns_to_ms(now_ns - self._last_ns) >= self._config.idle_expiry_ms

    # ---------------------------------------------------------------- support

    def support_ratio(self, now_ns: int) -> float:
        """The fraction of expected observation slots in the window that carried support.

        The window is the recent past: from the first observation, but never longer than
        `support_window_ms`. Slots are `support_slot_ms` wide — the rate observations actually
        arrive at — and a slot counts as supported when an observation inside it carried
        positive evidence (`signal > 0.5`).

        This is the defence against a flickering detector (04 §6.3). Four strong frames spread
        through fifteen seconds occupy four slots out of a hundred and fifty; twelve
        consecutive frames at 10 fps occupy twelve out of twelve.
        """
        if self._first_ns is None:
            return 0.0
        window_ms = min(
            ns_to_ms(now_ns - self._first_ns),
            self._config.support_window_ms,
        )
        if window_ms <= 0.0:
            return 1.0 if self._supporting_count(self._first_ns) else 0.0
        start_ns = now_ns - int(window_ms * NS_PER_MS)
        slots = max(1, math.ceil(window_ms / self._slot_ms))
        occupied = {
            min(int((s.t_monotonic_ns - start_ns) / (self._slot_ms * NS_PER_MS)), slots - 1)
            for s in self._samples
            if s.t_monotonic_ns >= start_ns and s.signal > NEUTRAL_SIGNAL
        }
        return len(occupied) / slots

    def _supporting_count(self, since_ns: int) -> int:
        return sum(
            1 for s in self._samples if s.t_monotonic_ns >= since_ns and s.signal > NEUTRAL_SIGNAL
        )

    # ---------------------------------------------------------------- state

    @property
    def state(self) -> AccumulatorState:
        return self.state_at(self._last_ns if self._last_ns is not None else 0)

    def state_at(self, now_ns: int) -> AccumulatorState:
        first = self._first_ns if self._first_ns is not None else 0
        last = self._last_ns if self._last_ns is not None else 0
        return AccumulatorState(
            camera_id=self._camera_id,
            module_id=self._module_id,
            subject=self._subject,
            log_odds=self._log_odds,
            confidence_ratio=confidence_of(
                self._log_odds, confidence_scale=self._config.confidence_scale
            ),
            observation_count=self._total_observations,
            support_ratio=self.support_ratio(max(now_ns, last)),
            first_observation_ns=first,
            last_observation_ns=last,
            is_active=self._active,
        )
