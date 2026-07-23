"""
Reports: persistence, month-over-month comparison, white-label PDF export,
critical-alert notifications, and public share-link management.

Flow: the wizard calls /api/upload/analyze first (stateless), lets the user
edit the AI text, then POSTs the final payload here to persist a `reports`
row. On save we automatically compute the comparison against the client's
previous report (if any) and fire off critical-alert notifications.
/api/reports/{id}/pdf renders and streams the richer branded PDF.
"""
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.db.supabase_client import get_current_agency_id, get_supabase
from app.models.schemas import ReportCreateRequest, ReportOut, ReportUpdate, ShareLinkUpdate
from app.services import comparison_service, notification_service
from app.services.feature_gate import check_limit
from app.services.pdf_service import generate_pdf_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[ReportOut])
async def list_reports(agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = sb.table("reports").select("*").eq("agency_id", agency_id).order("created_at", desc=True).execute()
    return res.data


@router.post("", response_model=ReportOut, status_code=201)
async def create_report(
    payload: ReportCreateRequest,
    agency_id: str = Depends(get_current_agency_id),
):
    """Persist the finalized report, compute MoM comparison, and notify on critical anomalies."""
    sb = get_supabase()

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    reports_this_month = len(
        sb.table("reports").select("id").eq("agency_id", agency_id).gte("created_at", month_start).execute().data or []
    )
    check_limit(agency_id, "max_reports_per_month", reports_this_month)

    previous = comparison_service.get_previous_report(
        agency_id=agency_id, client_id=payload.meta.client_id, platform=payload.meta.platform
    )
    comparison = comparison_service.build_comparison(payload.metrics, previous)

    row = {
        **payload.meta.model_dump(),
        "agency_id": agency_id,
        "metrics": payload.metrics,
        "daily_series": payload.daily_series,
        "anomalies": payload.anomalies,
        "comparison": comparison,
        "ai_summary": payload.ai_summary,
        "ai_recommendations": payload.ai_recommendations,
        "status": "ready",
    }
    res = sb.table("reports").insert(row).execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="Failed to save report")
    report = res.data[0]

    # Fire-and-forget style notification (best-effort, never blocks the save).
    try:
        agency_res = sb.table("agencies").select("notification_email, slack_webhook_url").eq("id", agency_id).single().execute()
        client_res = sb.table("clients").select("name").eq("id", payload.meta.client_id).single().execute()
        agency = agency_res.data or {}
        client = client_res.data or {}
        settings = get_settings()
        notification_service.notify_critical_anomalies(
            agency_notification_email=agency.get("notification_email"),
            slack_webhook_url=agency.get("slack_webhook_url"),
            client_name=client.get("name", "Client"),
            period_label=payload.meta.period_label,
            anomalies=payload.anomalies,
            report_url=f"{settings.FRONTEND_URL}/reports",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[reports] Notification dispatch failed (non-fatal): {exc}")

    return report


@router.patch("/{report_id}", response_model=ReportOut)
async def update_report(report_id: str, payload: ReportUpdate, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    res = sb.table("reports").update(updates).eq("id", report_id).eq("agency_id", agency_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Report not found")
    return res.data[0]


@router.patch("/{report_id}/share", response_model=ReportOut)
async def update_share_link(report_id: str, payload: ShareLinkUpdate, agency_id: str = Depends(get_current_agency_id)):
    """Enable/disable the public no-login share link for a report."""
    sb = get_supabase()
    res = (
        sb.table("reports")
        .update({"public_share_enabled": payload.public_share_enabled})
        .eq("id", report_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Report not found")
    return res.data[0]


@router.delete("/{report_id}", status_code=204)
async def delete_report(report_id: str, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    sb.table("reports").delete().eq("id", report_id).eq("agency_id", agency_id).execute()
    return None


def _render_and_store_pdf(sb, report: dict, agency: dict, client: dict, report_id: str, agency_id: str) -> bytes:
    pdf_bytes = generate_pdf_report(
        agency_name=agency.get("name", "Your Agency"),
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

    storage_path = f"{agency_id}/{report_id}.pdf"
    try:
        sb.storage.from_("reports-pdf").upload(
            storage_path, pdf_bytes, {"content-type": "application/pdf", "upsert": "true"}
        )
        sb.table("reports").update({"status": "exported", "pdf_url": storage_path}).eq("id", report_id).execute()
    except Exception as exc:  # noqa: BLE001 — don't block the download if storage write fails
        print(f"[reports] Storage upload failed (non-fatal): {exc}")

    return pdf_bytes


@router.get("/{report_id}/pdf")
async def export_pdf(report_id: str, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()

    report_res = sb.table("reports").select("*").eq("id", report_id).eq("agency_id", agency_id).single().execute()
    if not report_res.data:
        raise HTTPException(status_code=404, detail="Report not found")
    report = report_res.data

    client_res = sb.table("clients").select("*").eq("id", report["client_id"]).single().execute()
    client = client_res.data or {}

    agency_res = sb.table("agencies").select("*").eq("id", agency_id).single().execute()
    agency = agency_res.data or {}

    pdf_bytes = _render_and_store_pdf(sb, report, agency, client, report_id, agency_id)

    filename = f"{client.get('name', 'client').replace(' ', '_')}_report.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
