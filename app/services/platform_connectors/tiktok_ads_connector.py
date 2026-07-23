"""
TikTok Ads connector — STUB (Phase 3, not yet live).

Same rationale as meta_ads_connector.py: plumbed into the abstraction so
the rest of the system already treats TikTok as a real platform option,
but not implemented pending TikTok Marketing API app review approval.

Once approved, implement following the google_ads_connector.py pattern:
  - get_oauth_url: https://ads.tiktok.com/marketing_api/auth
  - exchange_code_for_tokens: POST https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/
  - refresh_access_token: TikTok access tokens are long-lived (no refresh
      token flow in the same sense) — re-check token validity rather than
      refreshing; re-auth via get_oauth_url if expired.
  - list_ad_accounts: GET /open_api/v1.3/oauth2/advertiser/get/
  - fetch_campaign_data: GET /open_api/v1.3/report/integrated/get/ with
      dimensions=["stat_time_day"], metrics=["impressions","clicks","spend",
      "conversion","conversion_value"], mapped into the same
      ParsedCampaignData shape as the other connectors.
"""
from __future__ import annotations

from app.models.schemas import ParsedCampaignData
from app.services.platform_connectors.base import (
    AdAccount,
    ConnectorNotAvailable,
    DateRange,
    PlatformConnector,
    TokenSet,
)

NOT_AVAILABLE_MESSAGE = (
    "TikTok Ads integration is not yet available — it's built into RAKH's "
    "connector framework but pending TikTok's Marketing API app review. "
    "Use CSV upload for TikTok campaigns in the meantime."
)


class TikTokAdsConnector(PlatformConnector):
    platform = "tiktok"

    def get_oauth_url(self, state: str) -> str:
        raise ConnectorNotAvailable(NOT_AVAILABLE_MESSAGE)

    def exchange_code_for_tokens(self, code: str) -> TokenSet:
        raise ConnectorNotAvailable(NOT_AVAILABLE_MESSAGE)

    def refresh_access_token(self, refresh_token: str) -> TokenSet:
        raise ConnectorNotAvailable(NOT_AVAILABLE_MESSAGE)

    def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        raise ConnectorNotAvailable(NOT_AVAILABLE_MESSAGE)

    def fetch_campaign_data(
        self,
        access_token: str,
        external_account_id: str,
        date_range: DateRange,
        target_ctr: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
    ) -> ParsedCampaignData:
        raise ConnectorNotAvailable(NOT_AVAILABLE_MESSAGE)
