"""Connector registry — one place to look up the right connector by platform name."""
from __future__ import annotations

from app.models.schemas import Platform
from app.services.platform_connectors.base import PlatformConnector
from app.services.platform_connectors.google_ads_connector import GoogleAdsConnector
from app.services.platform_connectors.meta_ads_connector import MetaAdsConnector
from app.services.platform_connectors.tiktok_ads_connector import TikTokAdsConnector

_CONNECTORS: dict[Platform, PlatformConnector] = {
    "google": GoogleAdsConnector(),
    "meta": MetaAdsConnector(),
    "tiktok": TikTokAdsConnector(),
}


def get_connector(platform: Platform) -> PlatformConnector:
    if platform not in _CONNECTORS:
        raise ValueError(f"Unknown platform: {platform}")
    return _CONNECTORS[platform]
