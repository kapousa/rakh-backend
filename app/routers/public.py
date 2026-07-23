"""
Public report sharing — no authentication required.

Anyone with the share token (a random UUID, unguessable) can view the
report and download its PDF, as long as the agency has toggled
`public_share_enabled` on for that report. This is the client-facing
"here's your live report link" feature — no login needed for the client.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import io

from app.db.supabase_client import get_supabase
from app.services import domain_service
from app.services.pdf_service import generate_pdf_report

router = APIRouter(prefix="/api/public", tags=["public"])


def _get_shared_report(token: str) -> dict:
    sb = get_supabase()
    res = (
        sb.table("reports")
        .select("*")
        .eq("public_share_token", token)
        .eq("public_share_enabled", True)
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="This report link is invalid or has been disabled.")
    return res.data


@router.get("/reports/{token}")
async def get_public_report(token: str):
    """Returns the report data (metrics, charts data, AI narrative) for public display."""
    sb = get_supabase()
    report = _get_shared_report(token)

    client_res = sb.table("clients").select("name, logo_url, brand_primary_color").eq("id", report["client_id"]).single().execute()
    agency_res = sb.table("agencies").select("name, logo_url").eq("id", report["agency_id"]).single().execute()

    return {
        "client": client_res.data or {},
        "agency": agency_res.data or {},
        "platform": report["platform"],
        "period_label": report.get("period_label"),
        "metrics": report.get("metrics", {}),
        "daily_series": report.get("daily_series", []),
        "anomalies": report.get("anomalies", []),
        "comparison": report.get("comparison", {}),
        "ai_summary": report.get("ai_summary"),
        "ai_recommendations": report.get("ai_recommendations", []),
        "pdf_theme": report.get("pdf_theme", "corporate_blue"),
    }


@router.get("/reports/{token}/pdf")
async def download_public_pdf(token: str):
    sb = get_supabase()
    report = _get_shared_report(token)

    client_res = sb.table("clients").select("*").eq("id", report["client_id"]).single().execute()
    agency_res = sb.table("agencies").select("*").eq("id", report["agency_id"]).single().execute()
    client = client_res.data or {}
    agency = agency_res.data or {}

    pdf_bytes = generate_pdf_report(
        agency_name=agency.get("name", "Agency"),
        agency_logo_url=agency.get("logo_url"),
        client_name=client.get("name", "Client"),
        client_logo_url=client.get("logo_url"),
        platform=report["platform"],
        period_label=report.get("period_label"),
        theme_key=report.get("pdf_theme", "corporate_blue"),
        metrics=report.get("metrics", {}),
        daily_series=report.get("daily_series", []),
        anomalies=report.get("anomalies", []),
        comparison=report.get("comparison", {}),
        ai_summary=report.get("ai_summary") or "",
        ai_recommendations=report.get("ai_recommendations") or [],
        language=report.get("language", "en"),
    )

    filename = f"{client.get('name', 'client').replace(' ', '_')}_report.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/branding")
async def get_branding_for_domain(domain: str | None = None):
    """
    Unauthenticated white-label branding lookup, used by the frontend's
    pre-login screen (and app shell before the agency's own data has
    loaded) to show a reseller's branding when accessed via their
    custom_domain — this is what makes the rebrand apply even before a
    user signs in, not just inside the authenticated app.

    Returns default (null) branding for the platform's own domain or any
    unrecognized host — the frontend falls back to its built-in RAKH
    branding in that case, which is the expected/normal path for the
    vast majority of visitors.
    """
    if not domain:
        return {"white_label_enabled": False}

    sb = get_supabase()
    res = (
        sb.table("agencies")
        .select("platform_display_name, platform_logo_url, platform_favicon_url, white_label_enabled")
        .eq("custom_domain", domain)
        .eq("white_label_enabled", True)
        .execute()
    )
    if not res.data:
        return {"white_label_enabled": False}

    return res.data[0]


@router.get("/domain-ask")
async def domain_ask_for_tls(domain: str):
    """
    Callback for a reverse proxy's "on-demand TLS" feature (e.g. Caddy's
    `ask` directive — see deploy/Caddyfile). Before automatically requesting
    a Let's Encrypt certificate for an arbitrary incoming domain, the proxy
    calls this endpoint; we return 200 only if that domain is a verified,
    white-label-enabled custom_domain in our database, and a non-2xx
    otherwise. This is the standard safe pattern for on-demand TLS — without
    it, anyone could point any domain at your IP and get free certificates
    minted on your infrastructure's behalf.
    """
    sb = get_supabase()
    if domain_service.is_domain_verified_for_agency(sb, domain):
        return {"ok": True}
    raise HTTPException(status_code=403, detail="Domain not verified for any agency")
