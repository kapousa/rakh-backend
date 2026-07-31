"""
TikTok Ads connector (Phase 3 — code complete, gated by TikTok Marketing
API review).

IMPORTANT: like the Meta connector, this is fully implemented but will NOT
work against real client ad accounts until your TikTok for Business app
has completed Marketing API review — an external approval process on
TikTok's side, not a code limitation here.

A second, separate caveat specific to this connector: TikTok's Marketing
API report field names have changed across API versions in the past, and
some fields — particularly the revenue/purchase-value field — vary
depending on what the advertiser has configured for conversion tracking
(Pixel events, catalog/shopping ads, etc.), similar to the action-type
ambiguity in the Meta connector. The field names below (`conversion`,
`total_complete_payment_value`) reflect the commonly documented v1.3
Marketing API basic reporting metrics — **verify these against TikTok's
current API reference** once you have real API access, since this
connector could not be tested against a live account while building it.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.schemas import Anomaly, Metrics, ParsedCampaignData
from app.services.anomaly_detector import detect_anomalies
from app.services.platform_connectors.base import AdAccount, DateRange, PlatformConnector, TokenSet

API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
OAUTH_AUTHORIZE_URL = "https://business-api.tiktok.com/portal/auth"
OAUTH_TOKEN_URL = f"{API_BASE}/oauth2/access_token/"
ADVERTISER_LIST_URL = f"{API_BASE}/oauth2/advertiser/get/"
REPORT_URL = f"{API_BASE}/report/integrated/get/"


class TikTokAdsConnector(PlatformConnector):
    platform = "tiktok"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _require_config(self) -> None:
        if not (self.settings.TIKTOK_OAUTH_APP_ID and self.settings.TIKTOK_OAUTH_APP_SECRET):
            raise RuntimeError(
                "TikTok Ads OAuth is not configured — set TIKTOK_OAUTH_APP_ID and "
                "TIKTOK_OAUTH_APP_SECRET in the backend .env (see README for setup)."
            )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def get_oauth_url(self, state: str) -> str:
        self._require_config()
        params = {
            "app_id": self.settings.TIKTOK_OAUTH_APP_ID,
            "redirect_uri": self.settings.TIKTOK_OAUTH_REDIRECT_URI,
            "state": state,
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, code: str) -> TokenSet:
        self._require_config()
        resp = httpx.post(
            OAUTH_TOKEN_URL,
            json={
                "app_id": self.settings.TIKTOK_OAUTH_APP_ID,
                "secret": self.settings.TIKTOK_OAUTH_APP_SECRET,
                "auth_code": code,
            },
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"TikTok token exchange failed: {body.get('message')}")

        data = body["data"]
        # TikTok access tokens are long-lived and don't expire on a fixed
        # schedule the way Google's do — there's no standard refresh_token
        # concept in the public Marketing API. We store the access token
        # in both fields for interface consistency with the other
        # connectors (see refresh_access_token below).
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data["access_token"],
            expires_in_seconds=None,  # TikTok tokens don't carry a standard expiry in the exchange response
        )

    def refresh_access_token(self, refresh_token: str) -> TokenSet:
        """TikTok tokens don't expire on a predictable schedule, so this
        doesn't re-exchange anything — it just verifies the existing token
        is still valid with a lightweight API call, and raises clearly if
        it's been revoked so the agency knows to reconnect."""
        self._require_config()
        resp = httpx.get(
            ADVERTISER_LIST_URL,
            params={"app_id": self.settings.TIKTOK_OAUTH_APP_ID, "secret": self.settings.TIKTOK_OAUTH_APP_SECRET},
            headers={"Access-Token": refresh_token},
            timeout=10.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(
                "TikTok access token appears to be invalid or revoked — the agency needs to reconnect this account."
            )
        return TokenSet(access_token=refresh_token, refresh_token=refresh_token, expires_in_seconds=None)

    # ------------------------------------------------------------------
    # Account listing
    # ------------------------------------------------------------------
    def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        self._require_config()
        resp = httpx.get(
            ADVERTISER_LIST_URL,
            params={"app_id": self.settings.TIKTOK_OAUTH_APP_ID, "secret": self.settings.TIKTOK_OAUTH_APP_SECRET},
            headers={"Access-Token": access_token},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"TikTok advertiser list failed: {body.get('message')}")

        advertisers = body.get("data", {}).get("list", [])
        return [
            AdAccount(external_id=str(a["advertiser_id"]), name=a.get("advertiser_name") or str(a["advertiser_id"]))
            for a in advertisers
        ]

    # ------------------------------------------------------------------
    # Campaign data — normalized into the same shape csv_parser.py produces
    # ------------------------------------------------------------------
    def fetch_campaign_data(
        self,
        access_token: str,
        external_account_id: str,
        date_range: DateRange,
        target_ctr: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
    ) -> ParsedCampaignData:
        self._require_config()

        resp = httpx.get(
            REPORT_URL,
            params={
                "advertiser_id": external_account_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_ADVERTISER",
                "dimensions": json.dumps(["stat_time_day"]),
                "metrics": json.dumps([
                    "impressions", "clicks", "spend", "conversion", "total_complete_payment_value",
                ]),
                "start_date": date_range.start.isoformat(),
                "end_date": date_range.end.isoformat(),
                "page": "1",
                "page_size": "500",
            },
            headers={"Access-Token": access_token},
            timeout=30.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"TikTok report fetch failed: {body.get('message')}")

        rows = body.get("data", {}).get("list", [])

        daily_series = []
        for row in rows:
            m = row.get("metrics", {})
            d = row.get("dimensions", {})
            impressions = int(float(m.get("impressions", 0)))
            clicks = int(float(m.get("clicks", 0)))
            spend = float(m.get("spend", 0))
            conversions = float(m.get("conversion", 0))
            ctr = round((clicks / impressions * 100), 2) if impressions else 0.0
            # stat_time_day comes back as "2026-08-01 00:00:00" — keep just the date part
            date_str = str(d.get("stat_time_day", "")).split(" ")[0]
            daily_series.append({
                "date": date_str,
                "impressions": impressions,
                "clicks": clicks,
                "spend": round(spend, 2),
                "conversions": round(conversions, 2),
                "ctr": ctr,
            })

        total_impressions = sum(r["impressions"] for r in daily_series)
        total_clicks = sum(r["clicks"] for r in daily_series)
        total_spend = sum(r["spend"] for r in daily_series)
        total_conversions = sum(r["conversions"] for r in daily_series)
        total_revenue = sum(float(row.get("metrics", {}).get("total_complete_payment_value", 0)) for row in rows)

        metrics = Metrics(
            impressions=total_impressions,
            clicks=total_clicks,
            spend=round(total_spend, 2),
            conversions=round(total_conversions, 2),
            ctr=round((total_clicks / total_impressions * 100), 2) if total_impressions else 0.0,
            cpc=round((total_spend / total_clicks), 2) if total_clicks else 0.0,
            cpa=round((total_spend / total_conversions), 2) if total_conversions else 0.0,
            roas=round((total_revenue / total_spend), 2) if total_spend else 0.0,
            revenue=round(total_revenue, 2),
        )

        anomalies: list[Anomaly] = detect_anomalies(
            metrics=metrics,
            daily_series=daily_series,
            target_ctr=target_ctr,
            target_cpa=target_cpa,
            target_roas=target_roas,
        )

        return ParsedCampaignData(
            platform="tiktok",
            metrics=metrics,
            daily_series=daily_series,
            anomalies=anomalies,
            rows_parsed=len(daily_series),
        )
