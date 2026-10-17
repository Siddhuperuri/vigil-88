"""Constant-velocity Kalman filter over a bounding box, in pure Python.

State is `(cx, cy, w, h)` and the velocity of each. With diagonal noise the full 8-dimensional
filter factorises exactly into four independent 2-state `(position, velocity)` filters, so this
is the textbook filter (the one in SORT, DeepSORT and ByteTrack), not an approximation of it,
and needs no linear-algebra library: a 2x2 covariance is three floats.

Time is explicit. The step is `dt` seconds, taken from frame timestamps, never an assumed frame
rate: inference runs at whatever rate the scheduler grants, and a filter that assumed a fixed
step would mis-predict every time that rate changed. Process noise is the continuous
white-noise-acceleration model, so its effect scales correctly with `dt`.

Noise is relative to the box (horizontal quantities scale with its width, vertical ones with its
height), so the same settings suit a near object and a far one.
"""

from __future__ import annotations

from vigil.core.geometry import BBox

_CHANNELS = 4  # cx, cy, w, h
_THIRD = 1.0 / 3.0


class BoxKalman:
    __slots__ = ("_acc", "_meas", "_min", "_pos", "_ppp", "_ppv", "_pvv", "_vel")

    def __init__(
        self,
        box: BBox,
        *,
        acceleration_noise_ratio: float,
        measurement_noise_ratio: float,
        min_size_px: float,
    ) -> None:
        self._acc = acceleration_noise_ratio
        self._meas = measurement_noise_ratio
        self._min = min_size_px
        z = _measurement(box)
        self._pos = list(z)
        self._vel = [0.0] * _CHANNELS
        scale = self._scale(z[2], z[3])
        # Position is known to within the detector's noise. Velocity is unknown: its prior
        # spread is the speed an object could build up in one second at the modelled
        # acceleration, so the first few measurements pin it down quickly.
        self._ppp = [(self._meas * s) ** 2 for s in scale]
        self._ppv = [0.0] * _CHANNELS
        self._pvv = [(self._acc * s) ** 2 for s in scale]

    def _scale(self, w: float, h: float) -> tuple[float, float, float, float]:
        w, h = max(w, self._min), max(h, self._min)
        return (w, h, w, h)

    def predict(self, dt_s: float) -> None:
        """Advance the state by `dt_s` seconds. A non-positive step leaves it unchanged."""
        if dt_s <= 0.0:
            return
        pos, vel, ppp, ppv, pvv = self._pos, self._vel, self._ppp, self._ppv, self._pvv
        scale = self._scale(pos[2], pos[3])
        dt2 = dt_s * dt_s
        dt3 = dt2 * dt_s
        for i in range(_CHANNELS):
            q = (self._acc * scale[i]) ** 2
            p_pp, p_pv, p_vv = ppp[i], ppv[i], pvv[i]
            ppp[i] = p_pp + 2.0 * dt_s * p_pv + dt2 * p_vv + q * dt3 * _THIRD
            ppv[i] = p_pv + dt_s * p_vv + q * dt2 * 0.5
            pvv[i] = p_vv + q * dt_s
            pos[i] += vel[i] * dt_s

    def update(self, box: BBox) -> None:
        z = _measurement(box)
        scale = self._scale(z[2], z[3])
        pos, vel, ppp, ppv, pvv = self._pos, self._vel, self._ppp, self._ppv, self._pvv
        for i in range(_CHANNELS):
            r = (self._meas * scale[i]) ** 2
            s = ppp[i] + r
            k_pos = ppp[i] / s
            k_vel = ppv[i] / s
            innovation = z[i] - pos[i]
            pos[i] += k_pos * innovation
            vel[i] += k_vel * innovation
            p_pv = ppv[i]
            ppp[i] = (1.0 - k_pos) * ppp[i]
            ppv[i] = (1.0 - k_pos) * p_pv
            pvv[i] -= k_vel * p_pv

    @property
    def box(self) -> BBox:
        """The current estimate, never smaller than the minimum box size."""
        cx, cy, w, h = self._pos
        w, h = max(w, self._min), max(h, self._min)
        return BBox(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)

    @property
    def velocity_px_s(self) -> tuple[float, float]:
        """Velocity of the box centre. Used for association, not reported: the reported velocity
        is a least-squares fit over matched detections (see `trajectory`), which does not depend
        on the filter's tuning."""
        return (self._vel[0], self._vel[1])


def _measurement(box: BBox) -> tuple[float, float, float, float]:
    return (
        (box.x1 + box.x2) / 2.0,
        (box.y1 + box.y2) / 2.0,
        box.x2 - box.x1,
        box.y2 - box.y1,
    )
