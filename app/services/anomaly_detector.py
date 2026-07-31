"""
Anomaly Detection & Alerting.

Two detection modes, applied together:

1. TARGET-BASED: if the client has KPI targets configured (target_ctr,
   target_cpa, target_roas), flag any metric that misses its target by
   a meaningful margin.

2. TREND-BASED (self-referential): using the daily series inside the
   uploaded file itself, compare the trailing half of the period against
   the leading half. A significant swing (e.g. CTR down 30%+, CPA up 30%+)
   is flagged even with no external target — this is what lets Roasify
   work standalone on a single CSV with zero prior history.

Findings are returned as `Anomaly` objects with a severity level so the
frontend can render badges and the LLM prompt can foreground them as
"Critical Action Items".
"""
from __future__ import annotations

from app.models.schemas import Anomaly, Metrics

# Thresholds for flagging a swing as warning vs. critical
WARNING_THRESHOLD_PCT = 15.0
CRITICAL_THRESHOLD_PCT = 30.0

# metric_name -> True if a drop is bad (lower is worse), False if a rise is bad
DROP_IS_BAD = {"ctr": True, "roas": True, "conversions": True, "cpa": False}


def _severity_for_delta(abs_delta_pct: float) -> str | None:
    if abs_delta_pct >= CRITICAL_THRESHOLD_PCT:
        return "critical"
    if abs_delta_pct >= WARNING_THRESHOLD_PCT:
        return "warning"
    return None


def _pct_change(before: float, after: float) -> float:
    if before == 0:
        return 0.0
    return round(((after - before) / before) * 100, 1)


def _target_based_anomalies(
    metrics: Metrics,
    target_ctr: float | None,
    target_cpa: float | None,
    target_roas: float | None,
) -> list[Anomaly]:
    findings: list[Anomaly] = []

    if target_ctr:
        delta = _pct_change(target_ctr, metrics.ctr)
        if delta < 0 and (sev := _severity_for_delta(abs(delta))):
            findings.append(
                Anomaly(
                    metric="ctr",
                    severity=sev,
                    delta_pct=delta,
                    message=f"CTR ({metrics.ctr}%) is {abs(delta)}% below the client's target of {target_ctr}%.",
                )
            )

    if target_cpa:
        delta = _pct_change(target_cpa, metrics.cpa)
        if delta > 0 and (sev := _severity_for_delta(abs(delta))):
            findings.append(
                Anomaly(
                    metric="cpa",
                    severity=sev,
                    delta_pct=delta,
                    message=f"CPA (${metrics.cpa}) is {abs(delta)}% above the client's target of ${target_cpa}.",
                )
            )

    if target_roas:
        delta = _pct_change(target_roas, metrics.roas)
        if delta < 0 and (sev := _severity_for_delta(abs(delta))):
            findings.append(
                Anomaly(
                    metric="roas",
                    severity=sev,
                    delta_pct=delta,
                    message=f"ROAS ({metrics.roas}x) is {abs(delta)}% below the client's target of {target_roas}x.",
                )
            )

    return findings


def _trend_based_anomalies(daily_series: list[dict]) -> list[Anomaly]:
    """Compare the first half vs. second half of the uploaded period."""
    findings: list[Anomaly] = []
    n = len(daily_series)
    if n < 4:
        return findings  # not enough data points for a meaningful trend split

    midpoint = n // 2
    first_half, second_half = daily_series[:midpoint], daily_series[midpoint:]

    def _avg(rows: list[dict], key: str) -> float:
        vals = [r[key] for r in rows if key in r]
        return sum(vals) / len(vals) if vals else 0.0

    for metric_key, label in (("ctr", "CTR"), ("spend", "Daily spend")):
        before, after = _avg(first_half, metric_key), _avg(second_half, metric_key)
        delta = _pct_change(before, after)
        bad_direction = delta < 0 if DROP_IS_BAD.get(metric_key, True) else delta > 0
        sev = _severity_for_delta(abs(delta))
        if sev and bad_direction:
            direction = "dropped" if delta < 0 else "spiked"
            findings.append(
                Anomaly(
                    metric=metric_key,
                    severity=sev,
                    delta_pct=delta,
                    message=(
                        f"{label} {direction} {abs(delta)}% in the second half of the "
                        f"period compared to the first half ({round(before, 2)} → {round(after, 2)})."
                    ),
                )
            )

    return findings


def detect_anomalies(
    metrics: Metrics,
    daily_series: list[dict],
    target_ctr: float | None = None,
    target_cpa: float | None = None,
    target_roas: float | None = None,
) -> list[Anomaly]:
    findings: list[Anomaly] = []
    findings.extend(_target_based_anomalies(metrics, target_ctr, target_cpa, target_roas))
    findings.extend(_trend_based_anomalies(daily_series))
    # Critical items first for UI/prompt prioritization
    findings.sort(key=lambda a: 0 if a.severity == "critical" else 1)
    return findings
