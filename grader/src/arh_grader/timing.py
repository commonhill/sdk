"""Timing: the grader measures everything itself, with a monotonic clock around each request.

`Timings` collects the measured batches (seconds, and how many items each had); `summary`
reduces a list of durations to median, p90 and the coefficient of variation (CV = sample standard
deviation / mean), the usual check of how noisy a measurement is.
"""

import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

clock = time.perf_counter  # monotonic, the highest resolution available


def percentile(values: Sequence[float], q: float) -> float:
    """The `q`-th percentile (0-100) by linear interpolation between closest ranks."""
    if not values:
        raise ValueError("no values")
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def cv(values: Sequence[float]) -> float:
    """Sample standard deviation / mean; 0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        raise ValueError("the mean is 0: CV undefined")
    return statistics.stdev(values) / mean


def summary(values: Sequence[float]) -> dict[str, float]:
    """n, total, mean, median, p90, min, max and CV of `values`."""
    return {
        "n": float(len(values)),
        "total": math.fsum(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(values, 90),
        "min": min(values),
        "max": max(values),
        "cv": cv(values),
    }


@dataclass
class Timings:
    """Measured batches: `record(seconds, n_items)` each, after any warmup (warmup batches are
    never recorded). Per-item times are batch time / items, the finest a batched request can
    resolve."""

    batch_s: list[float] = field(default_factory=list[float])
    items: list[int] = field(default_factory=list[int])

    def record(self, seconds: float, n_items: int) -> None:
        if seconds < 0 or n_items < 1:
            raise ValueError("a batch takes >= 0 s and has at least one item")
        self.batch_s.append(seconds)
        self.items.append(n_items)

    @property
    def total_s(self) -> float:
        return math.fsum(self.batch_s)

    @property
    def item_s(self) -> list[float]:
        return [s / n for s, n in zip(self.batch_s, self.items, strict=True)]

    def as_result(self) -> dict[str, float]:
        """Millisecond figures for result.json's `timings`: total, per batch and per item (median,
        p90) and the batch CV. Empty when nothing was measured."""
        if not self.batch_s:
            return {}
        batch, item = summary(self.batch_s), summary(self.item_s)
        return {
            "total_ms": self.total_s * 1000,
            "batches": batch["n"],
            "batch_median_ms": batch["median"] * 1000,
            "batch_p90_ms": batch["p90"] * 1000,
            "batch_cv": batch["cv"],
            "item_median_ms": item["median"] * 1000,
            "item_p90_ms": item["p90"] * 1000,
        }
