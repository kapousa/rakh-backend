"""
Google Ads connector (Phase 1).

The only connector live so far — Google Ads doesn't require the kind of
formal app review Meta and TikTok do for read-only reporting access at a
standard developer token tier, which is why this shipped first (see the
integration plan doc for the full comparison).

Auth: standard OAuth 2.0 authorization-code flow against Google's endpoints.
Data: Google Ads API v17 REST (`searchStream`) using GAQL (Google Ads Query
Language) to pull daily campaign-level metrics, aggregated the same way
`csv_parser.py` aggregates a manual CSV export.

NOTE: this requires a real Google Cloud OAuth client (Client ID/Secret) and
an approved Google Ads Developer Token to actually run against a live
account — see README for setup steps. Without those configured, connection
attempts fail with a clear config error rather than a confusing runtime one.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.schemas import Anomaly, Metrics, ParsedCampaignData
from app.services.anomaly_detector import detect_anomalies
from app.services.platform_connectors.base import AdAccount, DateRange, PlatformConnector, TokenSet

GOOGLE_OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ADS_API_VERSION = "v17"
GOOGLE_ADS_API_BASE = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"

# Read-only reporting scope — deliberately NOT requesting write/management
# access, per the least-privilege principle in the integration plan.
GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


class GoogleAdsConnector(PlatformConnector):
    platform = "google"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _require_config(self) -> None:
        if not (self.settings.GOOGLE_OAUTH_CLIENT_ID and self.settings.GOOGLE_OAUTH_CLIENT_SECRET):
            raise RuntimeError(
                "Google Ads OAuth is not configured — set GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET in the backend .env (see README for setup)."
            )
        if not self.settings.GOOGLE_ADS_DEVELOPER_TOKEN:
            raise RuntimeError(
                "GOOGLE_ADS_DEVELOPER_TOKEN is not set — required to call the Google Ads "
                "API, separate from the OAuth client credentials."
            )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def get_oauth_url(self, state: str) -> str:
        self._require_config()
        params = {
            "client_id": self.settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": self.settings.GOOGLE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": GOOGLE_ADS_SCOPE,
            "access_type": "offline",  # required to receive a refresh_token
            "prompt": "consent",       # forces refresh_token on repeat connections too
            "state": state,
        }
        return f"{GOOGLE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, code: str) -> TokenSet:
        self._require_config()
        resp = httpx.post(
            GOOGLE_OAUTH_TOKEN_URL,
            data={
                "client_id": self.settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": self.settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.GOOGLE_OAUTH_REDIRECT_URI,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in_seconds=data.get("expires_in"),
        )

    def refresh_access_token(self, refresh_token: str) -> TokenSet:
        self._require_config()
        resp = httpx.post(
            GOOGLE_OAUTH_TOKEN_URL,
            data={
                "client_id": self.settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": self.settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=refresh_token,  # Google doesn't rotate refresh tokens by default
            expires_in_seconds=data.get("expires_in"),
        )

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "developer-token": self.settings.GOOGLE_ADS_DEVELOPER_TOKEN,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Account listing
    # ------------------------------------------------------------------
    def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        self._require_config()
        resp = httpx.get(
            f"{GOOGLE_ADS_API_BASE}/customers:listAccessibleCustomers",
            headers=self._headers(access_token),
            timeout=15.0,
        )
        resp.raise_for_status()
        resource_names = resp.json().get("resourceNames", [])

        accounts: list[AdAccount] = []
        for resource_name in resource_names:
            customer_id = resource_name.split("/")[-1]
            name = self._fetch_customer_name(access_token, customer_id) or customer_id
            accounts.append(AdAccount(external_id=customer_id, name=name))
        return accounts

    def _fetch_customer_name(self, access_token: str, customer_id: str) -> str | None:
        """Best-effort lookup of a human-readable account name; falls back
        to the raw customer ID in the UI if this fails for any reason."""
        try:
            resp = httpx.post(
                f"{GOOGLE_ADS_API_BASE}/customers/{customer_id}/googleAds:search",
                headers=self._headers(access_token),
                json={"query": "SELECT customer.descriptive_name FROM customer LIMIT 1"},
                timeout=10.0,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0]["customer"]["descriptiveName"]
        except Exception as exc:  # noqa: BLE001
            print(f"[google_ads_connector] Could not fetch customer name for {customer_id}: {exc}")
        return None

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
        query = f"""
            SELECT
                segments.date,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value
            FROM campaign
            WHERE segments.date BETWEEN '{date_range.start.isoformat()}' AND '{date_range.end.isoformat()}'
            ORDER BY segments.date ASC
        """
        resp = httpx.post(
            f"{GOOGLE_ADS_API_BASE}/customers/{external_account_id}/googleAds:searchStream",
            headers=self._headers(access_token),
            json={"query": query},
            timeout=30.0,
        )
        resp.raise_for_status()

        # searchStream returns a JSON array of batches, each with a "results" list
        daily: dict[str, dict] = {}
        for batch in resp.json():
            for row in batch.get("results", []):
                d = row["segments"]["date"]
                m = row["metrics"]
                bucket = daily.setdefault(d, {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0, "revenue": 0.0})
                bucket["impressions"] += int(m.get("impressions", 0))
                bucket["clicks"] += int(m.get("clicks", 0))
                bucket["spend"] += int(m.get("costMicros", 0)) / 1_000_000  # micros -> currency units
                bucket["conversions"] += float(m.get("conversions", 0))
                bucket["revenue"] += float(m.get("conversionsValue", 0))

        daily_series = []
        for d in sorted(daily.keys()):
            row = daily[d]
            ctr = round((row["clicks"] / row["impressions"] * 100), 2) if row["impressions"] else 0.0
            daily_series.append({
                "date": d,
                "impressions": row["impressions"],
                "clicks": row["clicks"],
                "spend": round(row["spend"], 2),
                "conversions": round(row["conversions"], 2),
                "ctr": ctr,
            })

        total_impressions = sum(r["impressions"] for r in daily_series)
        total_clicks = sum(r["clicks"] for r in daily_series)
        total_spend = sum(r["spend"] for r in daily_series)
        total_conversions = sum(r["conversions"] for r in daily_series)
        total_revenue = sum(daily[d]["revenue"] for d in daily)

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
            platform="google",
            metrics=metrics,
            daily_series=daily_series,
            anomalies=anomalies,
            rows_parsed=len(daily_series),
        )
