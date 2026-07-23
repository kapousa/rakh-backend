"""
CSV / Excel ingestion service.

Handles the loose reality of ad-platform exports: inconsistent column
naming, extra header/footer rows, different date formats, and — critically
— different account currencies. Normalizes everything into the shared
`Metrics` + daily time series contract so the LLM and PDF layers never
need to know which platform (or currency, or column preset) the data
came from.
"""
from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

from app.models.schemas import Metrics, ParsedCampaignData, Platform
from app.services.anomaly_detector import detect_anomalies

# Column aliases seen across real-world exports from each platform.
# Keys are our canonical field names; values are lists of possible header
# spellings (lower-cased, whitespace-stripped) we should map from via exact
# match. For patterns that vary by a substituted value (mainly currency
# code in the spend column — Meta/Google export "Amount spent (USD)",
# "Amount spent (AED)", "Cost (SAR)", etc. depending on the ad account's
# currency), see SPEND_COLUMN_PATTERNS below instead — an exact-match list
# can never cover every currency code, so that one needs a regex.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["day", "date", "reporting starts", "date start"],
    "impressions": ["impressions", "impr."],
    "clicks": ["clicks", "clicks (all)", "link clicks"],
    "spend": ["cost", "spend", "amount spent"],
    "conversions": ["results", "conversions", "conversion", "purchases"],
    "revenue": ["conversion value", "purchase conversion value", "revenue", "conv. value"],
}

# Matches "amount spent (usd)", "amount spent (aed)", "cost (sar)", etc. —
# any currency code in parentheses, not just USD. This is what real Meta/
# Google exports actually look like once an ad account isn't in USD, which
# is the common case for agencies operating in the UAE/Gulf region.
SPEND_COLUMN_PATTERNS = [
    re.compile(r"^amount spent\s*\([a-z]{3}\)$"),
    re.compile(r"^cost\s*\([a-z]{3}\)$"),
]


def _canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename whatever headers are present to our canonical field names."""
    lower_map = {c: str(c).strip().lower() for c in df.columns}
    rename: dict[str, str] = {}

    for original, lowered in lower_map.items():
        # Currency-agnostic spend column match first (regex) — takes
        # priority since it's the more specific pattern.
        if any(pattern.match(lowered) for pattern in SPEND_COLUMN_PATTERNS):
            rename[original] = "spend"
            continue
        # Then exact-match aliases for everything else.
        for canonical, aliases in COLUMN_ALIASES.items():
            if lowered in aliases:
                rename[original] = canonical
                break

    df = df.rename(columns=rename)
    return df


def _read_any(file_bytes: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(file_bytes))
    # Try a couple of common encodings; ad platforms love UTF-8-BOM.
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(file_bytes), encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode file with any supported encoding.")


def parse_campaign_file(
    file_bytes: bytes,
    filename: str,
    platform: Platform,
    target_ctr: float | None = None,
    target_cpa: float | None = None,
    target_roas: float | None = None,
) -> ParsedCampaignData:
    """
    Parse a raw CSV/Excel export into normalized metrics + a daily time
    series, and run anomaly detection against the client's targets
    (or against the file's own trailing average if no targets are set).
    """
    df = _read_any(file_bytes, filename)
    df = _canonicalize_columns(df)

    # Only impressions and spend are truly non-negotiable — without spend
    # there's no ROAS/CPA story to tell, and without impressions there's no
    # reach story either. Clicks (and conversions/revenue) are legitimately
    # absent from some real-world export presets — e.g. Meta's "Results"-
    # focused column set (Reach/Frequency/Result type) doesn't include a
    # Clicks column at all. Those are handled by defaulting to 0 below
    # rather than rejecting the whole file over one missing metric.
    required_min = {"impressions", "spend"}
    missing = required_min - set(df.columns)
    if missing:
        raise ValueError(
            f"Uploaded file is missing required columns for platform '{platform}': "
            f"{', '.join(sorted(missing))}. Detected columns: {list(df.columns)}"
        )

    # Coerce numeric columns, filling absent optional ones with 0.
    for col in ("impressions", "clicks", "spend", "conversions", "revenue"):
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date")

    # ---- Aggregate totals -------------------------------------------------
    total_impressions = int(df["impressions"].sum())
    total_clicks = int(df["clicks"].sum())
    total_spend = float(df["spend"].sum())
    total_conversions = float(df["conversions"].sum())
    total_revenue = float(df["revenue"].sum())

    ctr = round((total_clicks / total_impressions * 100), 2) if total_impressions else 0.0
    cpc = round((total_spend / total_clicks), 2) if total_clicks else 0.0
    cpa = round((total_spend / total_conversions), 2) if total_conversions else 0.0
    roas = round((total_revenue / total_spend), 2) if total_spend else 0.0

    metrics = Metrics(
        impressions=total_impressions,
        clicks=total_clicks,
        spend=round(total_spend, 2),
        conversions=round(total_conversions, 2),
        ctr=ctr,
        cpc=cpc,
        cpa=cpa,
        roas=roas,
        revenue=round(total_revenue, 2),
    )

    # ---- Daily series for charting -----------------------------------------
    daily_series: list[dict[str, Any]] = []
    if "date" in df.columns and df["date"].notna().any():
        grouped = (
            df.groupby(df["date"].dt.date)
            .agg(
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
                spend=("spend", "sum"),
                conversions=("conversions", "sum"),
            )
            .reset_index()
        )
        for _, row in grouped.iterrows():
            daily_ctr = round((row["clicks"] / row["impressions"] * 100), 2) if row["impressions"] else 0.0
            daily_series.append(
                {
                    "date": str(row["date"]),
                    "impressions": int(row["impressions"]),
                    "clicks": int(row["clicks"]),
                    "spend": round(float(row["spend"]), 2),
                    "conversions": round(float(row["conversions"]), 2),
                    "ctr": daily_ctr,
                }
            )

    anomalies = detect_anomalies(
        metrics=metrics,
        daily_series=daily_series,
        target_ctr=target_ctr,
        target_cpa=target_cpa,
        target_roas=target_roas,
    )

    return ParsedCampaignData(
        platform=platform,
        metrics=metrics,
        daily_series=daily_series,
        anomalies=anomalies,
        rows_parsed=len(df),
    )
