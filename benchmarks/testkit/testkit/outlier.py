"""Outlier detection against the median of comparable values (same test, same rack or group).

A value is an outlier when it is more than pct percent worse than the median: lower for
bandwidth-like values, higher for latency-like values (lower_better).
"""


def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def threshold(med, pct, lower_better=False):
    return med * (1 + pct / 100.0) if lower_better else med * (1 - pct / 100.0)


def outliers(values, pct, lower_better=False, min_count=3):
    """{key: (value, median)} of values more than pct percent worse than the median of all values.

    values: {key: number}. Fewer than min_count values give no outliers (nothing to compare with).
    """
    vals = {k: v for k, v in values.items() if v is not None}
    if len(vals) < min_count:
        return {}
    med = median(vals.values())
    if not med:
        return {}
    limit = threshold(med, pct, lower_better)
    return {k: (v, med) for k, v in vals.items() if (v > limit if lower_better else v < limit)}


def deviation(value, med):
    """Percent difference from the median, e.g. -12.5."""
    return round((value - med) * 100.0 / med, 1) if med else 0.0
