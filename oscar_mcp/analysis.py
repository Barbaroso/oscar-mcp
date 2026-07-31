"""Aggregation helpers that turn raw OSCAR rows into LLM-friendly summaries."""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Sequence
from typing import Any

# Widely used interpretation aids. They are reference points for reading the
# numbers, not a diagnosis.
AHI_BANDS: tuple[tuple[str, float, float], ...] = (
    ("normal", 0.0, 5.0),
    ("mild", 5.0, 15.0),
    ("moderate", 15.0, 30.0),
    ("severe", 30.0, float("inf")),
)

COMPLIANCE_HOURS = 4.0
RESMED_LARGE_LEAK_LPM = 24.0

DISCLAIMER = (
    "Informational analysis of your own CPAP data. Not a medical device and not "
    "medical advice; discuss therapy changes with your clinician."
)

UNITS = {
    "ahi": "events/hour",
    "rdi": "events/hour",
    "total_hours": "hours",
    "mask_on_hours": "hours",
    "pressure_avg": "cmH2O",
    "pressure_min": "cmH2O",
    "pressure_max": "cmH2O",
    "pressure_95th": "cmH2O",
    "leak_total_avg": "L/min",
    "leak_total_95th": "L/min",
    "leak_total_max": "L/min",
    "spo2_avg": "%",
    "spo2_min": "%",
    "pulse_avg": "bpm",
}


def severity_band(ahi: float | None) -> str | None:
    """Map an AHI value onto the conventional severity bands."""
    if ahi is None:
        return None
    for name, low, high in AHI_BANDS:
        if low <= ahi < high:
            return name
    return None


def _numbers(rows: Iterable[dict], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def _oximetry(rows: Iterable[dict], key: str) -> list[float]:
    """Oximetry values, dropping zeros.

    OSCAR stores 0 when no oximeter was attached. Reporting that as a measured
    value would suggest an impossible saturation or pulse, so it is treated as
    missing data instead.
    """
    return [v for v in _numbers(rows, key) if v > 0]


def _stats(values: Sequence[float], digits: int = 2) -> dict | None:
    """Return a compact descriptive summary, or ``None`` when there is no data."""
    if not values:
        return None
    ordered = sorted(values)
    summary = {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), digits),
        "median": round(statistics.median(ordered), digits),
        "min": round(ordered[0], digits),
        "max": round(ordered[-1], digits),
    }
    if len(ordered) > 1:
        summary["stdev"] = round(statistics.stdev(ordered), digits)
    return summary


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolated percentile; ``pct`` is given as 0-100."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def ahi_distribution(values: Sequence[float]) -> dict[str, int]:
    counts = {name: 0 for name, _, _ in AHI_BANDS}
    for value in values:
        band = severity_band(value)
        if band:
            counts[band] += 1
    return counts


def trend(rows: Sequence[dict], key: str) -> dict | None:
    """Compare the first and second half of a period for a single metric.

    A halves comparison is used rather than a regression slope because it stays
    meaningful with the small number of nights typically available.
    """
    values = [(r["date"], float(r[key])) for r in rows if r.get(key) is not None]
    if len(values) < 4:
        return None

    values.sort(key=lambda item: item[0])
    midpoint = len(values) // 2
    first = [v for _, v in values[:midpoint]]
    second = [v for _, v in values[len(values) - midpoint :]]

    first_mean = statistics.fmean(first)
    second_mean = statistics.fmean(second)
    change = second_mean - first_mean
    pct = (change / first_mean * 100.0) if first_mean else None

    # Treat small movements as flat so short, noisy periods aren't over-read.
    if abs(change) < 1e-9 or (pct is not None and abs(pct) < 5):
        direction = "stable"
    else:
        direction = "increasing" if change > 0 else "decreasing"

    return {
        "metric": key,
        "unit": UNITS.get(key),
        "first_half_mean": round(first_mean, 2),
        "second_half_mean": round(second_mean, 2),
        "change": round(change, 2),
        "change_pct": round(pct, 1) if pct is not None else None,
        "direction": direction,
    }


def summarise_nights(rows: Sequence[dict], start: dt.date | None, end: dt.date | None) -> dict:
    """Build the aggregate report returned by the statistics tool."""
    usage = _numbers(rows, "total_hours")
    ahi = _numbers(rows, "ahi")
    compliant = [h for h in usage if h >= COMPLIANCE_HOURS]

    span_days = None
    if start and end:
        span_days = (end - start).days + 1

    events = {
        "obstructive": sum(int(r.get("obstructive_count") or 0) for r in rows),
        "hypopnea": sum(int(r.get("hypopnea_count") or 0) for r in rows),
        "clear_airway": sum(int(r.get("clear_airway_count") or 0) for r in rows),
        "unclassified": sum(int(r.get("unclassified_count") or 0) for r in rows),
        "rera": sum(int(r.get("rera_count") or 0) for r in rows),
    }

    leak_95 = _numbers(rows, "leak_total_95th")
    report: dict[str, Any] = {
        "period": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "days_in_period": span_days,
            "nights_with_data": len(rows),
            "nights_without_data": (span_days - len(rows)) if span_days else None,
        },
        "usage": {
            "hours": _stats(usage),
            "total_hours": round(sum(usage), 2) if usage else 0.0,
            "compliant_nights": len(compliant),
            "compliance_rate_pct": round(len(compliant) / len(usage) * 100, 1) if usage else None,
            "compliance_threshold_hours": COMPLIANCE_HOURS,
        },
        "ahi": {
            "stats": _stats(ahi),
            "p95": round(percentile(ahi, 95), 2) if ahi else None,
            "nights_by_severity": ahi_distribution(ahi),
            "overall_band": severity_band(statistics.fmean(ahi)) if ahi else None,
        },
        "events_total": events,
        "pressure": {
            "avg": _stats(_numbers(rows, "pressure_avg")),
            "p95": _stats(_numbers(rows, "pressure_95th")),
            "max": _stats(_numbers(rows, "pressure_max")),
        },
        "leak": {
            "avg": _stats(_numbers(rows, "leak_total_avg")),
            "p95": _stats(leak_95),
            "nights_above_large_leak_threshold": sum(
                1 for v in leak_95 if v >= RESMED_LARGE_LEAK_LPM
            ),
            "large_leak_threshold_lpm": RESMED_LARGE_LEAK_LPM,
        },
        "oximetry": {
            "spo2_avg": _stats(_oximetry(rows, "spo2_avg")),
            "spo2_min": _stats(_oximetry(rows, "spo2_min")),
            "pulse_avg": _stats(_oximetry(rows, "pulse_avg")),
        },
        "trends": [
            t
            for t in (
                trend(rows, "ahi"),
                trend(rows, "total_hours"),
                trend(rows, "leak_total_95th"),
                trend(rows, "pressure_95th"),
            )
            if t
        ],
        "reference": {
            "ahi_bands": {name: f"{low}-{high}" for name, low, high in AHI_BANDS},
            "units": UNITS,
        },
        "disclaimer": DISCLAIMER,
    }

    spo2_avg = _oximetry(rows, "spo2_avg")
    report["oximetry"] = {
        "available": bool(spo2_avg or _oximetry(rows, "pulse_avg")),
        "spo2_avg": _stats(spo2_avg),
        "spo2_min": _stats(_oximetry(rows, "spo2_min")),
        "pulse_avg": _stats(_oximetry(rows, "pulse_avg")),
    }
    if not report["oximetry"]["available"]:
        report["oximetry"]["note"] = "No oximeter data recorded for this period."

    if not rows:
        report["note"] = "No nights with data in the requested period."
    return report


def hourly_histogram(events: Sequence[dict], key: str = "start_time") -> dict[str, int]:
    """Count events per clock hour to expose clustering within a night."""
    buckets: dict[str, int] = {}
    for event in events:
        value = event.get(key)
        if value is None:
            continue
        hour = dt.datetime.fromtimestamp(value / 1000.0).strftime("%H:00")
        buckets[hour] = buckets.get(hour, 0) + 1
    return dict(sorted(buckets.items()))
