"""
Platform connector abstraction.

Every ad platform (Google, Meta, TikTok) implements this same interface.
The critical design decision: `fetch_campaign_data()` returns the exact
same `ParsedCampaignData` shape that `csv_parser.py` already produces for
manual uploads. That means the anomaly detector, comparison service, LLM
service, and PDF generator need zero changes to support auto-pulled data —
they've never known or cared whether the data came from a CSV or a live
API call.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from app.models.schemas import ParsedCampaignData, Platform


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in_seconds: int | None  # None if the platform doesn't return an expiry


@dataclass
class AdAccount:
    """A single ad account the OAuth grant has access to, shown to the
    agency so they can pick which one maps to a given client."""
    external_id: str
    name: str


@dataclass
class DateRange:
    start: date
    end: date


class PlatformConnector(ABC):
    platform: Platform

    @abstractmethod
    def get_oauth_url(self, state: str) -> str:
        """Build the URL to redirect the agency's browser to for consent."""

    @abstractmethod
    def exchange_code_for_tokens(self, code: str) -> TokenSet:
        """Exchange the OAuth authorization code for access/refresh tokens."""

    @abstractmethod
    def refresh_access_token(self, refresh_token: str) -> TokenSet:
        """Use a refresh token to obtain a fresh access token."""

    @abstractmethod
    def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        """List the ad accounts this token can access, for the account-picker UI."""

    @abstractmethod
    def fetch_campaign_data(
        self,
        access_token: str,
        external_account_id: str,
        date_range: DateRange,
        target_ctr: float | None = None,
        target_cpa: float | None = None,
        target_roas: float | None = None,
    ) -> ParsedCampaignData:
        """
        Pull campaign performance data and normalize it into the shared
        ParsedCampaignData shape — identical to what csv_parser.py produces,
        so every downstream service (anomaly detection, comparison, LLM
        narrative, PDF) works the same regardless of data source.

        target_ctr/cpa/roas are the client's configured KPI targets (see
        Client.target_ctr etc.) — passed through to detect_anomalies() for
        target-based anomaly flagging, matching the CSV upload path. Trend-
        based detection (via the daily series) always runs regardless.
        """


class ConnectorNotAvailable(NotImplementedError):
    """Raised by platforms that are plumbed into the abstraction but not
    yet live because their app review hasn't been approved (Meta, TikTok)."""
