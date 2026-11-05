"""Statistics that are computed in one pass over a stream, in memory that does not grow with it.

Three small accumulators, each of a fixed size however many values are fed to it:

- ``OnlineMoments``: count, mean, standard deviation, skewness and excess kurtosis, with the
  one-pass update of Welford and Pébay, which stays accurate where the textbook sums of squares
  lose all their digits (values such as 1 000 000 000 + x).
- ``P2QuantileEstimator`` and ``QuantileSummary``: the quartiles, estimated with the piecewise
  parabolic (P²) algorithm of Jain and Chlamtac (1985). They are estimates, not exact values.
- ``HyperLogLogSketch``: the number of distinct values, within about 6.5 % (256 registers, 256
  bytes), using 64-bit BLAKE2b hashes.

None of them keeps a value it was given. The first five values of a quantile estimator are held
until it can start, which is its whole state and is not reported.
"""

from __future__ import annotations

import math
from hashlib import blake2b

_HLL_INDEX_BITS = 8
_HLL_REGISTERS = 1 << _HLL_INDEX_BITS
_HLL_REST_BITS = 64 - _HLL_INDEX_BITS
_HLL_ALPHA = 0.7213 / (1.0 + 1.079 / _HLL_REGISTERS)


class OnlineMoments:
    """Running count, mean and central moments of a stream of finite numbers."""

    __slots__ = ("count", "m2", "m3", "m4", "mean")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.m3 = 0.0
        self.m4 = 0.0

    def update(self, x: float) -> None:
        """Take one value in; NaN and infinities are ignored."""
        if math.isnan(x) or math.isinf(x):
            return
        n1 = self.count
        self.count += 1
        n = self.count
        delta = x - self.mean
        delta_n = delta / n
        delta_n2 = delta_n * delta_n
        term1 = delta * delta_n * n1
        self.mean += delta_n
        self.m4 += term1 * delta_n2 * (n * n - 3 * n + 3) + 6 * delta_n2 * self.m2 - 4 * delta_n * self.m3
        self.m3 += term1 * delta_n * (n - 2) - 3 * delta_n * self.m2
        self.m2 += term1

    def to_dict(self) -> dict[str, float | None]:
        """Mean, sample standard deviation, skewness and excess kurtosis; None where undefined."""
        count = self.count
        stddev = round(math.sqrt(self.m2 / (count - 1)), 4) if count >= 2 else None
        skewness = round((math.sqrt(count) * self.m3) / (self.m2**1.5), 4) if count >= 3 and self.m2 > 0 else None
        kurtosis = (
            round((count * self.m4) / (self.m2 * self.m2) - 3.0, 4) if count >= 4 and self.m2 > 0 else None
        )
        return {
            "mean": round(self.mean, 4),
            "stddev": stddev,
            "skewness": skewness,
            "excess_kurtosis": kurtosis,
        }


class P2QuantileEstimator:
    """One quantile ``p`` of a stream, by the P² algorithm: five markers, nothing else is kept."""

    __slots__ = ("_desired", "_heights", "_increments", "_p", "_positions", "_start", "count")

    def __init__(self, p: float) -> None:
        if not 0.0 < p < 1.0:
            raise ValueError("p must lie between 0 and 1, exclusive")
        self._p = p
        self.count = 0
        self._start: list[float] = []
        self._heights: list[float] = []
        self._positions = [1, 2, 3, 4, 5]
        self._desired = [1.0, 1.0 + 2.0 * p, 1.0 + 4.0 * p, 3.0 + 2.0 * p, 5.0]
        self._increments = (0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0)

    def update(self, x: float) -> None:
        """Take one value in; NaN and infinities are ignored."""
        if math.isnan(x) or math.isinf(x):
            return
        self.push(x)

    def push(self, x: float) -> None:
        """``update`` for a value already known to be finite (the hot path of ``QuantileSummary``)."""
        self.count += 1
        q = self._heights
        if not q:
            start = self._start
            start.append(x)
            if len(start) == 5:
                self._heights = sorted(start)
                self._start = []
            return
        n = self._positions
        if x < q[0]:
            q[0] = x
            n[1] += 1
            n[2] += 1
            n[3] += 1
            n[4] += 1
        elif x < q[1]:
            n[1] += 1
            n[2] += 1
            n[3] += 1
            n[4] += 1
        elif x < q[2]:
            n[2] += 1
            n[3] += 1
            n[4] += 1
        elif x < q[3]:
            n[3] += 1
            n[4] += 1
        else:
            if x > q[4]:
                q[4] = x
            n[4] += 1
        desired = self._desired
        increments = self._increments
        desired[1] += increments[1]
        desired[2] += increments[2]
        desired[3] += increments[3]
        for i in (1, 2, 3):
            d = desired[i] - n[i]
            if d >= 1.0:
                if n[i + 1] - n[i] > 1:
                    self._move(i, 1)
            elif d <= -1.0 and n[i - 1] - n[i] < -1:
                self._move(i, -1)

    def _move(self, i: int, step: int) -> None:
        """Shift interior marker ``i`` one position by the parabolic formula, or the linear one."""
        q = self._heights
        n = self._positions
        parabolic = q[i] + step / (n[i + 1] - n[i - 1]) * (
            (n[i] - n[i - 1] + step) * (q[i + 1] - q[i]) / (n[i + 1] - n[i])
            + (n[i + 1] - n[i] - step) * (q[i] - q[i - 1]) / (n[i] - n[i - 1])
        )
        if not q[i - 1] < parabolic < q[i + 1]:  # the parabola left the markers' order: go straight
            parabolic = q[i] + step * (q[i + step] - q[i]) / (n[i + step] - n[i])
        q[i] = parabolic
        n[i] += step
    def value(self) -> float | None:
        """The estimate; None before any value, and exact (nearest rank) while there are fewer than five."""
        if self.count == 0:
            return None
        if not self._heights:
            ordered = sorted(self._start)
            return ordered[min(len(ordered) - 1, int(self._p * len(ordered)))]
        return self._heights[2]


class QuantileSummary:
    """The quartiles of a stream: three P² estimators.

    P² is exact only in the limit and is known to be less accurate on a stream that is sorted or
    drifts steadily; the answer is an estimate either way.
    """

    __slots__ = ("_q25", "_q50", "_q75")

    def __init__(self) -> None:
        self._q25 = P2QuantileEstimator(0.25)
        self._q50 = P2QuantileEstimator(0.50)
        self._q75 = P2QuantileEstimator(0.75)

    def update(self, x: float) -> None:
        if math.isnan(x) or math.isinf(x):
            return
        self._q25.push(x)
        self._q50.push(x)
        self._q75.push(x)
    def to_dict(self) -> dict[str, float]:
        """``p25``, ``median``, ``p75`` and ``iqr`` (estimates); empty before any value."""
        low, middle, high = self._q25.value(), self._q50.value(), self._q75.value()
        if low is None or middle is None or high is None:
            return {}
        return {
            "p25": round(low, 4),
            "median": round(middle, 4),
            "p75": round(high, 4),
            "iqr": round(high - low, 4),
        }


class HyperLogLogSketch:
    """Approximate count of distinct values in 256 bytes (standard error about 6.5 %)."""

    __slots__ = ("_registers",)

    def __init__(self) -> None:
        self._registers = bytearray(_HLL_REGISTERS)

    def add(self, value_bytes: bytes) -> None:
        digest = int.from_bytes(blake2b(value_bytes, digest_size=8).digest(), "big")
        index = digest >> _HLL_REST_BITS
        rest = digest & ((1 << _HLL_REST_BITS) - 1)
        rank = (_HLL_REST_BITS - rest.bit_length()) + 1  # 57 when every remaining bit is 0
        if rank > self._registers[index]:
            self._registers[index] = rank

    def estimate(self) -> int:
        """The estimated number of distinct values added; 0 if none was."""
        registers = self._registers
        zeros = registers.count(0)
        if zeros == _HLL_REGISTERS:
            return 0
        harmonic = sum(2.0 ** -register for register in registers)
        raw = _HLL_ALPHA * _HLL_REGISTERS * _HLL_REGISTERS / harmonic
        if raw <= 2.5 * _HLL_REGISTERS and zeros > 0:  # few values: counting the empty registers is closer
            raw = _HLL_REGISTERS * math.log(_HLL_REGISTERS / zeros)
        return max(1, round(raw))
