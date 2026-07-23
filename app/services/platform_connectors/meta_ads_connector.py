"""
Meta Ads connector — STUB (Phase 2, not yet live).

This exists so the connector abstraction and all downstream plumbing
(routers, sync service, frontend account-picker) already know about Meta
as a platform option. It's intentionally not implemented yet: Meta's
Marketing API requires App Review (screencast, use-case writeup) plus
Business Verification of the agency in Meta Business Manager before any
real client data can be pulled — see the integration plan doc.

Once that approval lands, implement each method following the same
pattern as google_ads_connector.py:
  - get_oauth_url: https://www.facebook.com/v21.0/dialog/oauth
      scope=ads_read (read-only — do NOT request ads_management)
  - exchange_code_for_tokens: https://graph.facebook.com/v21.0/oauth/access_token
  - refresh_access_token: Meta long-lived tokens work differently from
      Google's refresh-token model — exchange a short-lived token for a
      long-lived one via /oauth/access_token?grant_type=fb_exchange_token,
      then re-exchange before the ~60 day expiry rather than "refreshing"
      in the traditional sense.
  - list_ad_accounts: GET /me/adaccounts
  - fetch_campaign_data: GET /{ad_account_id}/insights with
      fields=impressions,clicks,spend,actions,action_values and
      time_range + time_increment=1 for a daily breakdown, mapped into
      the same ParsedCampaignData shape as the Google connector.
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
    "Meta Ads integration is not yet available — it's built into RAKH's "
    "connector framework but pending Meta's App Review and Business "
    "Verification process. Use CSV upload for Meta campaigns in the meantime."
)


class MetaAdsConnector(PlatformConnector):
    platform = "meta"

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
