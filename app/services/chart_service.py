"""
Chart rendering service.

Produces PNG chart images (as bytes) for embedding in the PDF report and,
optionally, exposing to the frontend. Using matplotlib here (instead of
ReportLab's native chart flowables) gives noticeably more polished output
for the "richer PDF" tier — smoother lines, proper legends, brand-colored
fills — at the cost of a slightly heavier dependency.
"""
from __future__ import annotations

import io
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless rendering, no display server needed
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.edgecolor": "#E5E7EB",
        "axes.labelcolor": "#374151",
        "xtick.color": "#6B7280",
        "ytick.color": "#6B7280",
    }
)


def _fig_to_png_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_spend_trend_chart(daily_series: list[dict[str, Any]], accent_color: str) -> bytes | None:
    """Line chart of daily spend over the reporting period."""
    if not daily_series:
        return None

    dates, spend = [], []
    for row in daily_series:
        try:
            dates.append(datetime.fromisoformat(str(row["date"])))
            spend.append(row.get("spend", 0))
        except (ValueError, KeyError):
            continue
    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ax.plot(dates, spend, color=accent_color, linewidth=2.2, marker="o", markersize=3)
    ax.fill_between(dates, spend, color=accent_color, alpha=0.08)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_ylabel("Daily Spend ($)")
    fig.autofmt_xdate(rotation=30)
    return _fig_to_png_bytes(fig)


def render_ctr_trend_chart(daily_series: list[dict[str, Any]], accent_color: str) -> bytes | None:
    """Line chart of daily CTR — the metric most anomaly alerts hinge on."""
    if not daily_series:
        return None

    dates, ctr = [], []
    for row in daily_series:
        try:
            dates.append(datetime.fromisoformat(str(row["date"])))
            ctr.append(row.get("ctr", 0))
        except (ValueError, KeyError):
            continue
    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ax.plot(dates, ctr, color="#DC2626", linewidth=2.2, marker="o", markersize=3)
    ax.fill_between(dates, ctr, color="#DC2626", alpha=0.07)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_ylabel("CTR (%)")
    fig.autofmt_xdate(rotation=30)
    return _fig_to_png_bytes(fig)


def render_comparison_bar_chart(comparison: dict[str, Any], accent_color: str) -> bytes | None:
    """Horizontal bar chart of % change per metric vs. the previous period."""
    deltas = comparison.get("deltas") if comparison else None
    if not deltas:
        return None

    labels = list(deltas.keys())
    values = [deltas[k]["pct_change"] for k in labels]
    colors = ["#059669" if deltas[k]["is_improvement"] else "#DC2626" for k in labels]

    fig, ax = plt.subplots(figsize=(7.2, 2.8))
    bars = ax.barh(labels, values, color=colors, height=0.55)
    ax.axvline(0, color="#9CA3AF", linewidth=0.8)
    for bar, val in zip(bars, values):
        ax.text(
            val + (1.5 if val >= 0 else -1.5),
            bar.get_y() + bar.get_height() / 2,
            f"{val:+.1f}%",
            va="center",
            ha="left" if val >= 0 else "right",
            fontsize=8.5,
            color="#374151",
        )
    ax.set_xlabel("% change vs. previous period")
    return _fig_to_png_bytes(fig)
