"""
Meta Ads connector (Phase 2 — code complete, gated by Meta App Review).

IMPORTANT: this connector is fully implemented and follows the exact same
pattern as google_ads_connector.py, but it will NOT work against real
client ad accounts until your Meta app has completed:
  1. Business Verification of your agency in Meta Business Manager
  2. App Review approval for the `ads_read` permission (screencast +
     use-case writeup — Meta reviews this manually)
This is an external approval process, not a code limitation — see the
integration plan doc for the full comparison against Google Ads (which
needed no such review for read-only reporting access).

Auth model differs from Google in one important way: Meta doesn't have a
traditional refresh_token. A short-lived token from the OAuth exchange is
immediately exchanged for a long-lived token (~60 days), and "refreshing"
means re-exchanging that same long-lived token for a new one before it
expires — there's no separate refresh credential. To fit the shared
PlatformConnector interface (which expects a distinct access/refresh pair),
this connector stores the same long-lived token in both fields; see
refresh_access_token() below for exactly how that's used.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.models.schemas import Anomaly, Metrics, ParsedCampaignData
from app.services.anomaly_detector import detect_anomalies
from app.services.platform_connectors.base import AdAccount, DateRange, PlatformConnector, TokenSet

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
OAUTH_AUTHORIZE_URL = f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth"
OAUTH_TOKEN_URL = f"{GRAPH_API_BASE}/oauth/access_token"

# Read-only ads scope — deliberately NOT requesting ads_management, same
# least-privilege principle as the Google connector's adwords scope choice.
META_SCOPE = "ads_read"

# Meta's Insights "actions" array can contain dozens of action types
# depending on what the campaign is optimizing for (purchases, leads, app
# installs, registrations...). There's no single canonical "conversions"
# field the way Google Ads has metrics.conversions, so this priority list
# picks the most likely "real" conversion event, matching the same
# ambiguity CSV exports have with Meta's "Results" column (which is
# whatever the campaign's chosen optimization goal happens to be).
CONVERSION_ACTION_PRIORITY = [
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "lead",
    "omni_complete_registration",
    "offsite_conversion.fb_pixel_lead",
]


class MetaAdsConnector(PlatformConnector):
    platform = "meta"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _require_config(self) -> None:
        if not (self.settings.META_OAUTH_APP_ID and self.settings.META_OAUTH_APP_SECRET):
            raise RuntimeError(
                "Meta Ads OAuth is not configured — set META_OAUTH_APP_ID and "
                "META_OAUTH_APP_SECRET in the backend .env (see README for setup)."
            )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def get_oauth_url(self, state: str) -> str:
        self._require_config()
        params = {
            "client_id": self.settings.META_OAUTH_APP_ID,
            "redirect_uri": self.settings.META_OAUTH_REDIRECT_URI,
            "state": state,
            "scope": META_SCOPE,
            "response_type": "code",
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, code: str) -> TokenSet:
        self._require_config()
        # Step 1: exchange the authorization code for a short-lived token.
        resp = httpx.get(
            OAUTH_TOKEN_URL,
            params={
                "client_id": self.settings.META_OAUTH_APP_ID,
                "client_secret": self.settings.META_OAUTH_APP_SECRET,
                "redirect_uri": self.settings.META_OAUTH_REDIRECT_URI,
                "code": code,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        short_lived_token = resp.json()["access_token"]

        # Step 2: immediately exchange for a long-lived token (~60 days)
        # so the agency isn't forced to re-auth every hour.
        return self._exchange_for_long_lived(short_lived_token)

    def _exchange_for_long_lived(self, current_token: str) -> TokenSet:
        resp = httpx.get(
            OAUTH_TOKEN_URL,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": self.settings.META_OAUTH_APP_ID,
                "client_secret": self.settings.META_OAUTH_APP_SECRET,
                "fb_exchange_token": current_token,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        long_lived_token = data["access_token"]
        return TokenSet(
            access_token=long_lived_token,
            # No distinct refresh token in Meta's model — store the same
            # long-lived token here so _get_valid_access_token() (which
            # calls refresh_access_token(refresh_token) uniformly across
            # every connector) has something valid to re-exchange later.
            refresh_token=long_lived_token,
            expires_in_seconds=data.get("expires_in"),  # typically ~5,184,000s (60 days)
        )

    def refresh_access_token(self, refresh_token: str) -> TokenSet:
        """`refresh_token` here is actually the previous long-lived access
        token (see class docstring) — re-exchange it for a new one before
        the 60-day expiry. If it's already expired, this call fails and
        the agency needs to reconnect via the full OAuth flow again;
        there's no way around that with Meta's token model."""
        self._require_config()
        return self._exchange_for_long_lived(refresh_token)

    def _params(self, access_token: str) -> dict[str, str]:
        return {"access_token": access_token}

    # ------------------------------------------------------------------
    # Account listing
    # ------------------------------------------------------------------
    def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        self._require_config()
        resp = httpx.get(
            f"{GRAPH_API_BASE}/me/adaccounts",
            params={**self._params(access_token), "fields": "id,name,account_id"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [
            AdAccount(external_id=acc["id"], name=acc.get("name") or acc["id"])
            for acc in data
        ]

    # ------------------------------------------------------------------
    # Campaign data — normalized into the same shape csv_parser.py produces
    # ------------------------------------------------------------------
    def _extract_conversions_and_value(self, insight_row: dict) -> tuple[float, float]:
        """Pick the best-matching conversion action type from Meta's
        actions/action_values arrays. See CONVERSION_ACTION_PRIORITY above."""
        actions = {a["action_type"]: float(a.get("value", 0)) for a in insight_row.get("actions", [])}
        values = {a["action_type"]: float(a.get("value", 0)) for a in insight_row.get("action_values", [])}

        for action_type in CONVERSION_ACTION_PRIORITY:
            if action_type in actions:
                return actions[action_type], values.get(action_type, 0.0)
        return 0.0, 0.0

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
        account_path = external_account_id if external_account_id.startswith("act_") else f"act_{external_account_id}"

        resp = httpx.get(
            f"{GRAPH_API_BASE}/{account_path}/insights",
            params={
                **self._params(access_token),
                "fields": "impressions,clicks,spend,actions,action_values",
                "time_range": f'{{"since":"{date_range.start.isoformat()}","until":"{date_range.end.isoformat()}"}}',
                "time_increment": "1",  # daily breakdown, matching the CSV parser's daily_series
                "level": "account",
                "limit": "500",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])

        daily_series = []
        for row in rows:
            impressions = int(row.get("impressions", 0))
            clicks = int(row.get("clicks", 0))
            spend = float(row.get("spend", 0))
            conversions, _ = self._extract_conversions_and_value(row)
            ctr = round((clicks / impressions * 100), 2) if impressions else 0.0
            daily_series.append({
                "date": row["date_start"],
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
        total_revenue = sum(self._extract_conversions_and_value(row)[1] for row in rows)

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
            platform="meta",
            metrics=metrics,
            daily_series=daily_series,
            anomalies=anomalies,
            rows_parsed=len(daily_series),
        )
