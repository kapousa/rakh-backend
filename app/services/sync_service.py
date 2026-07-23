"""
Sync orchestration service.

This is where an auto-pulled connection turns into a finished report,
reusing the exact same analysis pipeline manual CSV uploads go through —
anomaly detection, comparison, LLM narrative, PDF generation are all
untouched by whether the data came from a file or a live API call.

SAFETY DEFAULT: auto-generated reports do NOT send themselves to the
client immediately. They're created with review_status='pending_review'
and a review_send_at timestamp `client.review_window_hours` in the future
(default 24h). The agency gets notified immediately that a report is
ready to review; if nobody touches it, `send_due_reports()` (called by the
scheduler) auto-sends it once the window passes — "fully automatic" in
practice, but with a built-in pause instead of a blind, instant send.
Agencies can flip `client.auto_send_reports` off entirely to require
explicit manual approval on every report, or call the approve endpoint to
send immediately without waiting out the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.db.supabase_client import get_supabase
from app.services import comparison_service, notification_service
from app.services.llm_service import generate_report_analysis
from app.services.pdf_service import generate_pdf_report
from app.services.platform_connectors import get_connector
from app.services.platform_connectors.base import DateRange
from app.services.token_encryption import decrypt_token, encrypt_token


def _get_valid_access_token(sb, connection: dict) -> str:
    """Decrypt the stored access token, refreshing first if it's expired."""
    connector = get_connector(connection["platform"])
    expires_at = connection.get("token_expires_at")

    is_expired = False
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        is_expired = datetime.now(timezone.utc) >= expires_dt - timedelta(minutes=5)

    if not is_expired:
        return decrypt_token(connection["access_token_encrypted"])

    refresh_token = decrypt_token(connection["refresh_token_encrypted"])
    new_tokens = connector.refresh_access_token(refresh_token)

    new_expires_at = None
    if new_tokens.expires_in_seconds:
        new_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=new_tokens.expires_in_seconds)).isoformat()

    sb.table("ad_platform_connections").update({
        "access_token_encrypted": encrypt_token(new_tokens.access_token),
        "token_expires_at": new_expires_at,
    }).eq("id", connection["id"]).execute()

    return new_tokens.access_token


async def _pull_and_analyze(
    sb,
    connection: dict,
    client: dict,
    date_range: DateRange,
    language: str = "en",
    tone: str = "professional",
    period_label: str | None = None,
) -> dict:
    """
    Shared core: decrypt/refresh token, pull live data, run anomaly
    detection + comparison + LLM narrative. Used by both the background
    auto-sync job (run_sync_for_connection, which also saves + queues for
    review) and the wizard's on-demand preview (preview_connection_pull,
    which is stateless — same contract as /api/upload/analyze so the
    wizard's Step 3->4 flow works identically for both data sources).
    """
    connector = get_connector(connection["platform"])
    access_token = _get_valid_access_token(sb, connection)

    parsed = connector.fetch_campaign_data(
        access_token,
        connection["external_account_id"],
        date_range,
        target_ctr=client.get("target_ctr"),
        target_cpa=client.get("target_cpa"),
        target_roas=client.get("target_roas"),
    )

    previous = comparison_service.get_previous_report(
        agency_id=connection["agency_id"], client_id=client["id"], platform=connection["platform"]
    )
    comparison = comparison_service.build_comparison(parsed.metrics.model_dump(), previous)

    resolved_period_label = period_label or f"{date_range.start.strftime('%b %d')} – {date_range.end.strftime('%b %d, %Y')}"
    analysis = await generate_report_analysis(
        platform=connection["platform"],
        period_label=resolved_period_label,
        metrics=parsed.metrics,
        anomalies=parsed.anomalies,
        tone=tone,
        language=language,
        client_name=client["name"],
    )

    return {
        "parsed": parsed,
        "comparison": comparison,
        "analysis": analysis,
        "period_label": resolved_period_label,
    }


async def preview_connection_pull(
    connection_id: str,
    agency_id: str,
    date_range: DateRange,
    language: str = "en",
    tone: str = "professional",
    period_label: str | None = None,
) -> dict:
    """
    Stateless pull — used by the Report Wizard's "Pull from connected
    account" path. Does NOT save anything or touch review/send state; the
    agency reviews/edits in the wizard exactly like a CSV upload, then
    saves via the normal POST /api/reports flow. This is what makes the
    connection hybrid rather than purely automatic: the same live data
    source works for both the scheduled background sync (review-queued)
    and an on-demand, human-reviewed-from-the-start pull.
    """
    sb = get_supabase()

    conn_res = sb.table("ad_platform_connections").select("*").eq("id", connection_id).eq("agency_id", agency_id).single().execute()
    if not conn_res.data:
        raise ValueError(f"Connection {connection_id} not found")
    connection = conn_res.data

    client_res = sb.table("clients").select("*").eq("id", connection["client_id"]).single().execute()
    client = client_res.data
    if not client:
        raise ValueError(f"Client {connection['client_id']} not found")

    result = await _pull_and_analyze(sb, connection, client, date_range, language, tone, period_label)
    parsed = result["parsed"]

    return {
        "platform": parsed.platform,
        "rows_parsed": parsed.rows_parsed,
        "metrics": parsed.metrics.model_dump(),
        "daily_series": parsed.daily_series,
        "anomalies": [a.model_dump() for a in parsed.anomalies],
        "comparison": result["comparison"],
        "ai_summary": result["analysis"]["summary"],
        "ai_recommendations": result["analysis"]["recommendations"],
        "period_label": result["period_label"],
    }


async def run_sync_for_connection(connection_id: str, date_range: DateRange) -> dict:
    """
    Background version — pulls fresh data for one connection and saves a
    report in 'pending_review' status (see module docstring for the
    review-before-send safety default). Called by the scheduler; not used
    by the wizard's on-demand path (see preview_connection_pull above).
    """
    sb = get_supabase()
    settings = get_settings()

    conn_res = sb.table("ad_platform_connections").select("*").eq("id", connection_id).single().execute()
    if not conn_res.data:
        raise ValueError(f"Connection {connection_id} not found")
    connection = conn_res.data

    client_res = sb.table("clients").select("*").eq("id", connection["client_id"]).single().execute()
    client = client_res.data
    if not client:
        raise ValueError(f"Client {connection['client_id']} not found")

    result = await _pull_and_analyze(sb, connection, client, date_range)
    parsed = result["parsed"]
    comparison = result["comparison"]
    analysis = result["analysis"]
    period_label = result["period_label"]

    review_window_hours = client.get("review_window_hours", 24)
    review_send_at = (datetime.now(timezone.utc) + timedelta(hours=review_window_hours)).isoformat()

    row = {
        "agency_id": connection["agency_id"],
        "client_id": client["id"],
        "platform": connection["platform"],
        "period_label": period_label,
        "language": "en",
        "tone": "professional",
        "pdf_theme": "corporate_blue",
        "metrics": parsed.metrics.model_dump(),
        "daily_series": parsed.daily_series,
        "anomalies": [a.model_dump() for a in parsed.anomalies],
        "comparison": comparison,
        "ai_summary": analysis["summary"],
        "ai_recommendations": analysis["recommendations"],
        "status": "ready",
        "source": "auto_sync",
        "connection_id": connection_id,
        "review_status": "pending_review",
        "review_send_at": review_send_at,
    }
    report_res = sb.table("reports").insert(row).execute()
    report = report_res.data[0]

    sb.table("ad_platform_connections").update({
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_sync_status": "success",
        "last_sync_error": None,
    }).eq("id", connection_id).execute()

    # Notify the agency a new auto-generated report is waiting for review.
    try:
        agency_res = sb.table("agencies").select("notification_email").eq("id", connection["agency_id"]).single().execute()
        agency_email = (agency_res.data or {}).get("notification_email")
        notification_service.notify_report_pending_review(
            agency_notification_email=agency_email,
            client_name=client["name"],
            period_label=period_label,
            review_url=f"{settings.FRONTEND_URL}/reports",
            auto_send_hours=review_window_hours if client.get("auto_send_reports") else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[sync_service] Pending-review notification failed (non-fatal): {exc}")

    return report


def send_report_now(report_id: str) -> dict:
    """Render the PDF, email it to the client, enable the share link, and
    mark the report 'sent'. Used both by the manual 'approve & send' action
    and by the scheduler once a review window elapses."""
    sb = get_supabase()

    report_res = sb.table("reports").select("*").eq("id", report_id).single().execute()
    report = report_res.data
    client_res = sb.table("clients").select("*").eq("id", report["client_id"]).single().execute()
    client = client_res.data
    agency_res = sb.table("agencies").select("*").eq("id", report["agency_id"]).single().execute()
    agency = agency_res.data

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

    updates = {
        "review_status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "public_share_enabled": True,  # give the client a live link alongside the emailed PDF
    }
    updated_res = sb.table("reports").update(updates).eq("id", report_id).execute()
    updated_report = updated_res.data[0]

    if client.get("contact_email"):
        share_url = f"{get_settings().FRONTEND_URL}/share/{updated_report['public_share_token']}"
        notification_service.send_report_to_client(
            client_email=client["contact_email"],
            client_name=client["name"],
            agency_name=agency.get("name", "Your Agency"),
            period_label=report.get("period_label"),
            pdf_bytes=pdf_bytes,
            share_url=share_url,
        )

    return updated_report


def run_due_syncs() -> None:
    """Called by the scheduler. Finds every connection due for a sync based
    on its frequency and last_synced_at, and runs it."""
    import asyncio
    from datetime import date

    sb = get_supabase()
    now = datetime.now(timezone.utc)

    connections_res = sb.table("ad_platform_connections").select("*").eq("sync_enabled", True).execute()

    for connection in connections_res.data or []:
        frequency_days = 7 if connection["sync_frequency"] == "weekly" else 30
        last_synced = connection.get("last_synced_at")

        if last_synced:
            last_synced_dt = datetime.fromisoformat(last_synced.replace("Z", "+00:00"))
            if now - last_synced_dt < timedelta(days=frequency_days):
                continue

        date_range = DateRange(start=(now - timedelta(days=frequency_days)).date(), end=now.date())
        try:
            asyncio.run(run_sync_for_connection(connection["id"], date_range))
        except Exception as exc:  # noqa: BLE001 — one failing connection shouldn't stop the sweep
            print(f"[sync_service] Sync failed for connection {connection['id']}: {exc}")
            sb.table("ad_platform_connections").update({
                "last_sync_status": "failed",
                "last_sync_error": str(exc),
            }).eq("id", connection["id"]).execute()


def send_due_reports() -> None:
    """Called by the scheduler (more frequently than run_due_syncs — e.g.
    hourly). Auto-sends any pending_review report whose window has passed,
    but only for clients that opted into auto_send_reports; otherwise the
    report just waits for a human to hit 'Approve & Send' in the UI."""
    sb = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()

    due_res = (
        sb.table("reports")
        .select("id, client_id")
        .eq("review_status", "pending_review")
        .lte("review_send_at", now_iso)
        .execute()
    )

    for report in due_res.data or []:
        client_res = sb.table("clients").select("auto_send_reports").eq("id", report["client_id"]).single().execute()
        if not (client_res.data or {}).get("auto_send_reports"):
            continue  # stays pending_review until a human approves it
        try:
            send_report_now(report["id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[sync_service] Auto-send failed for report {report['id']}: {exc}")
