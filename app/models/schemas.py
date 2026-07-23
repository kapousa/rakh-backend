"""Pydantic request/response models."""
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal["meta", "google", "tiktok"]
Language = Literal["en", "ar"]
Tone = Literal["aggressive", "professional", "casual"]
PdfTheme = Literal["corporate_blue", "fresh_mint", "modern_minimalist"]


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
class ClientCreate(BaseModel):
    name: str
    industry: str | None = None
    contact_email: str | None = None
    logo_url: str | None = None
    brand_primary_color: str = "#4F46E5"
    brand_secondary_color: str = "#10B981"
    target_ctr: float | None = None
    target_cpa: float | None = None
    target_roas: float | None = None
    report_cadence_days: int | None = None  # e.g. 30 = monthly reminder cadence
    auto_send_reports: bool = False          # if true, auto-synced reports send after the review window instead of staying pending
    review_window_hours: int = 24            # how long an auto-generated report waits before auto-sending


class ClientUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    contact_email: str | None = None
    logo_url: str | None = None
    brand_primary_color: str | None = None
    brand_secondary_color: str | None = None
    target_ctr: float | None = None
    target_cpa: float | None = None
    target_roas: float | None = None
    report_cadence_days: int | None = None
    auto_send_reports: bool | None = None
    review_window_hours: int | None = None
    is_active: bool | None = None


class ClientOut(ClientCreate):
    id: str
    agency_id: str
    is_active: bool
    last_reminder_sent_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Ad platform integrations
# ---------------------------------------------------------------------------
SyncFrequency = Literal["weekly", "monthly"]
ReviewStatus = Literal["not_applicable", "pending_review", "approved", "held", "sent"]


class OAuthUrlOut(BaseModel):
    oauth_url: str


class AdAccountOut(BaseModel):
    external_id: str
    name: str


class ConnectionOut(BaseModel):
    id: str
    agency_id: str
    client_id: str
    platform: Platform
    external_account_id: str | None
    external_account_name: str | None
    sync_enabled: bool
    sync_frequency: SyncFrequency
    last_synced_at: datetime | None
    last_sync_status: Literal["success", "failed", "pending"] | None
    last_sync_error: str | None
    created_at: datetime


class ConnectionUpdate(BaseModel):
    sync_enabled: bool | None = None
    sync_frequency: SyncFrequency | None = None


class SelectAccountRequest(BaseModel):
    external_account_id: str
    external_account_name: str


class ReportReviewAction(BaseModel):
    action: Literal["approve", "hold"]


# ---------------------------------------------------------------------------
# Team / Agency
# ---------------------------------------------------------------------------
Role = Literal["owner", "admin", "member"]


class AgencyUpdate(BaseModel):
    name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    notification_email: str | None = None
    slack_webhook_url: str | None = None
    # White-label reseller fields — Enterprise-gated, see feature_gate.py.
    # Regular agencies can send these but the backend rejects the write
    # unless their plan has "platform_rebrand" enabled.
    white_label_enabled: bool | None = None
    platform_display_name: str | None = None
    platform_logo_url: str | None = None
    platform_favicon_url: str | None = None
    custom_domain: str | None = None


class AgencyOut(BaseModel):
    id: str
    name: str
    logo_url: str | None
    primary_color: str
    secondary_color: str
    notification_email: str | None
    slack_webhook_url: str | None
    plan: str
    plan_id: str | None = None
    plan_features: dict[str, Any] = {}
    reports_used_this_month: int = 0
    white_label_enabled: bool = False
    platform_display_name: str | None = None
    platform_logo_url: str | None = None
    platform_favicon_url: str | None = None
    custom_domain: str | None = None
    custom_domain_verified: bool = False
    custom_domain_verified_at: datetime | None = None


class DomainVerificationOut(BaseModel):
    verified: bool
    found: str | None
    expected: str
    error: str | None


class MemberInvite(BaseModel):
    email: str
    role: Role = "member"


class MemberOut(BaseModel):
    id: str
    agency_id: str
    user_id: str | None
    invited_email: str | None
    role: Role
    status: Literal["active", "pending"]
    created_at: datetime


class MemberRoleUpdate(BaseModel):
    role: Role


# ---------------------------------------------------------------------------
# Metrics / Anomalies (shared data contracts between parser, LLM, PDF)
# ---------------------------------------------------------------------------
class Metrics(BaseModel):
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    conversions: float = 0.0
    ctr: float = 0.0          # %
    cpc: float = 0.0
    cpa: float = 0.0
    roas: float = 0.0
    revenue: float = 0.0


class Anomaly(BaseModel):
    metric: str
    severity: Literal["info", "warning", "critical"]
    message: str
    delta_pct: float


class ParsedCampaignData(BaseModel):
    platform: Platform
    metrics: Metrics
    daily_series: list[dict[str, Any]]
    anomalies: list[Anomaly]
    rows_parsed: int


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
class ReportCreateMeta(BaseModel):
    client_id: str
    platform: Platform
    period_label: str | None = None
    language: Language = "en"
    tone: Tone = "professional"
    pdf_theme: PdfTheme = "corporate_blue"
    # Set by the wizard when the data came from a connected account rather
    # than a CSV upload. Still goes through the normal /api/reports save —
    # unlike the background scheduler's auto-sync path, a human already
    # reviewed this in the wizard, so it does NOT enter the pending_review
    # queue (review_status stays 'not_applicable', the DB default).
    source: Literal["manual_upload", "auto_sync"] = "manual_upload"
    connection_id: str | None = None


class ConnectionPreviewRequest(BaseModel):
    """Body for POST /api/integrations/connections/{id}/preview — the
    wizard's on-demand 'pull from connected account' path."""
    start_date: date
    end_date: date
    language: Language = "en"
    tone: Tone = "professional"
    period_label: str | None = None


class ReportCreateRequest(BaseModel):
    """
    Single wrapper model for POST /api/reports.

    NOTE: this used to be several separate function parameters (meta, metrics,
    daily_series, anomalies, ai_summary, ai_recommendations) directly on the
    route. FastAPI only infers "this belongs in the JSON body" for Pydantic
    models / dict / list[dict] params — plain `str` and `list[str]` params
    default to query parameters instead. That mismatch caused ai_summary to
    be expected as a URL query string, which resulted in a false "Field
    required" 422 even though the frontend was sending it correctly in the
    body. Wrapping everything in one explicit model avoids the ambiguity.
    """
    meta: ReportCreateMeta
    metrics: dict[str, Any]
    daily_series: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    ai_summary: str
    ai_recommendations: list[str]


class ReportUpdate(BaseModel):
    ai_summary: str | None = None
    ai_recommendations: list[str] | None = None
    pdf_theme: PdfTheme | None = None
    status: str | None = None


class ReportOut(BaseModel):
    id: str
    agency_id: str
    client_id: str
    platform: Platform
    period_label: str | None
    language: Language
    tone: Tone
    pdf_theme: PdfTheme
    metrics: dict[str, Any]
    daily_series: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]
    comparison: dict[str, Any] = {}
    ai_summary: str | None
    ai_recommendations: list[Any]
    status: str
    pdf_url: str | None
    public_share_token: str | None = None
    public_share_enabled: bool = False
    source: Literal["manual_upload", "auto_sync"] = "manual_upload"
    connection_id: str | None = None
    review_status: ReviewStatus = "not_applicable"
    review_send_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime


class ShareLinkUpdate(BaseModel):
    public_share_enabled: bool


# ---------------------------------------------------------------------------
# Plans & feature gating
# ---------------------------------------------------------------------------
class PlanFeatures(BaseModel):
    """
    The feature flag map stored on each plan. Extend this as the product
    grows — services/feature_gate.py reads these keys to decide what an
    agency can access, and the admin dashboard renders a toggle/number
    input per field automatically from this schema.
    """
    max_reports_per_month: int = 5   # -1 = unlimited
    max_team_members: int = 1        # -1 = unlimited
    max_clients: int = 3             # -1 = unlimited
    connected_accounts: bool = False
    auto_sync: bool = False
    critical_alerts: bool = False
    public_share_links: bool = True
    platform_rebrand: bool = False
    custom_domain: bool = False


class PlanCreate(BaseModel):
    key: str
    name: str
    monthly_price_usd: float = 0
    sort_order: int = 0
    is_active: bool = True
    features: PlanFeatures = PlanFeatures()


class PlanUpdate(BaseModel):
    name: str | None = None
    monthly_price_usd: float | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    features: PlanFeatures | None = None


class PlanOut(BaseModel):
    id: str
    key: str
    name: str
    monthly_price_usd: float
    is_active: bool
    sort_order: int
    features: dict[str, Any]
    created_at: datetime


class AgencyPlanAssignment(BaseModel):
    plan_id: str


class AdminAgencyOut(BaseModel):
    """Agency summary shown in the super-admin dashboard's agency list."""
    id: str
    name: str
    plan_id: str
    plan_key: str | None = None
    plan_name: str | None = None
    created_at: datetime


class PlatformAdminCheck(BaseModel):
    is_platform_admin: bool


# ---------------------------------------------------------------------------
# White-label reseller settings (Enterprise-gated)
# ---------------------------------------------------------------------------
class WhiteLabelSettings(BaseModel):
    white_label_enabled: bool = False
    platform_display_name: str | None = None
    platform_logo_url: str | None = None
    platform_favicon_url: str | None = None
    custom_domain: str | None = None
