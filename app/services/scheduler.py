"""
Recurring report reminder scheduler.

Runs a nightly job (when ENABLE_SCHEDULER=true) that checks every client
with a `report_cadence_days` set and sends the agency a reminder email if
their most recent report for that client is older than the cadence — the
practical version of "scheduled/automated recurring reports" for a
zero-ads-API product: we can't auto-pull new platform data without Ads API
access, but we CAN proactively prompt the agency to upload the next export
right on schedule, and store `last_reminder_sent_at` so we don't spam them.

This is wired into the FastAPI app lifespan in main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.supabase_client import get_supabase
from app.services.notification_service import notify_reminder

scheduler = AsyncIOScheduler()


def _run_reminder_sweep() -> None:
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    clients_res = (
        sb.table("clients")
        .select("id, name, agency_id, report_cadence_days, last_reminder_sent_at")
        .eq("is_active", True)
        .not_.is_("report_cadence_days", "null")
        .execute()
    )

    for client in clients_res.data or []:
        cadence_days = client.get("report_cadence_days")
        if not cadence_days:
            continue

        last_sent = client.get("last_reminder_sent_at")
        if last_sent:
            last_sent_dt = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
            if now - last_sent_dt < timedelta(days=cadence_days):
                continue  # already reminded within this cadence window

        # Find the most recent report for this client to know how "due" they are.
        last_report_res = (
            sb.table("reports")
            .select("created_at")
            .eq("client_id", client["id"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        last_report_at = None
        if last_report_res.data:
            last_report_at = datetime.fromisoformat(last_report_res.data[0]["created_at"].replace("Z", "+00:00"))

        is_due = last_report_at is None or (now - last_report_at) >= timedelta(days=cadence_days)
        if not is_due:
            continue

        agency_res = sb.table("agencies").select("notification_email").eq("id", client["agency_id"]).single().execute()
        agency_email = (agency_res.data or {}).get("notification_email")

        notify_reminder(agency_notification_email=agency_email, client_name=client["name"])
        sb.table("clients").update({"last_reminder_sent_at": now.isoformat()}).eq("id", client["id"]).execute()


def start_scheduler() -> None:
    from app.services.sync_service import run_due_syncs, send_due_reports

    # Reminder sweep: nightly at 09:00 UTC.
    scheduler.add_job(_run_reminder_sweep, "cron", hour=9, minute=0, id="report_reminder_sweep", replace_existing=True)

    # Ad platform sync sweep: nightly at 03:00 UTC (ahead of the reminder
    # sweep, and outside typical business hours since it hits external APIs).
    scheduler.add_job(run_due_syncs, "cron", hour=3, minute=0, id="ad_platform_sync_sweep", replace_existing=True)

    # Auto-send check: hourly — catches pending_review reports whose review
    # window has just elapsed, without waiting for the next daily cycle.
    scheduler.add_job(send_due_reports, "cron", minute=0, id="auto_send_due_reports", replace_existing=True)

    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
