from __future__ import annotations
from math import erf, sqrt
from .models import StatDistribution

def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))

def normalize_percentile(value: float, dist: StatDistribution) -> float:
    z = (value - dist.mean) / max(dist.std, 1e-6)
    return max(0.0, min(1.0, norm_cdf(z)))
