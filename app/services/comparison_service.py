"""
Month-over-Month Comparison Service.

Looks up the client's most recent prior report on the same platform and
computes percentage deltas for each headline metric. This is what powers
the "▲ 12% vs last period" badges in the wizard/report view and the
comparison table in the PDF.
"""
from __future__ import annotations

from typing import Any

from app.db.supabase_client import get_supabase

COMPARABLE_METRICS = ["impressions", "clicks", "spend", "conversions", "ctr", "cpc", "cpa", "roas", "revenue"]

# For CPA/CPC, a *decrease* is the improvement (cheaper), so we flip the
# "is this good news" direction relative to the raw sign of the delta.
LOWER_IS_BETTER = {"cpa", "cpc", "spend"}


def _pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None  # avoid divide-by-zero / meaningless infinite deltas
    return round(((after - before) / before) * 100, 1)


def get_previous_report(agency_id: str, client_id: str, platform: str, exclude_report_id: str | None = None) -> dict | None:
    """Fetch the most recent prior report for this client+platform, if any."""
    sb = get_supabase()
    query = (
        sb.table("reports")
        .select("id, metrics, period_label, created_at")
        .eq("agency_id", agency_id)
        .eq("client_id", client_id)
        .eq("platform", platform)
        .order("created_at", desc=True)
        .limit(5)  # small buffer in case the most recent is the one being excluded
    )
    res = query.execute()
    for row in res.data or []:
        if exclude_report_id and row["id"] == exclude_report_id:
            continue
        return row
    return None


def build_comparison(current_metrics: dict[str, Any], previous_report: dict | None) -> dict[str, Any]:
    """
    Returns a dict like:
    {
      "has_previous": true,
      "previous_period_label": "May 2026",
      "deltas": {
        "spend": {"before": 1200.0, "after": 1275.0, "pct_change": 6.3, "is_improvement": false},
        ...
      }
    }
    """
    if not previous_report:
        return {"has_previous": False}

    prev_metrics = previous_report.get("metrics", {})
    deltas: dict[str, Any] = {}

    for key in COMPARABLE_METRICS:
        before = float(prev_metrics.get(key, 0) or 0)
        after = float(current_metrics.get(key, 0) or 0)
        pct = _pct_change(before, after)
        if pct is None:
            continue

        is_lower_better = key in LOWER_IS_BETTER
        is_improvement = (pct < 0) if is_lower_better else (pct > 0)

        deltas[key] = {
            "before": round(before, 2),
            "after": round(after, 2),
            "pct_change": pct,
            "is_improvement": is_improvement,
        }

    return {
        "has_previous": True,
        "previous_period_label": previous_report.get("period_label"),
        "deltas": deltas,
    }
