"""
Notification service.

Sends critical-alert notifications (email + optional Slack webhook) when a
saved report contains critical anomalies, and generic "report ready" pings.
Both channels are best-effort and fully optional — if an agency hasn't
configured a notification_email or slack_webhook_url, we just skip silently
so this never blocks the core save/export flow.
"""
from __future__ import annotations

import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

from app.core.config import get_settings


def _send_email(to_email: str, subject: str, html_body: str) -> bool:
    settings = get_settings()
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD):
        print("[notifications] SMTP not configured — skipping email send.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL or settings.SMTP_USER
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(msg["From"], [to_email], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001 — notifications must never break the main flow
        print(f"[notifications] Email send failed: {exc}")
        return False


def _send_slack(webhook_url: str, text: str) -> bool:
    try:
        resp = httpx.post(webhook_url, json={"text": text}, timeout=6.0)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notifications] Slack webhook failed: {exc}")
        return False


def notify_critical_anomalies(
    *,
    agency_notification_email: str | None,
    slack_webhook_url: str | None,
    client_name: str,
    period_label: str | None,
    anomalies: list[dict[str, Any]],
    report_url: str | None = None,
) -> None:
    critical = [a for a in anomalies if a.get("severity") == "critical"]
    if not critical:
        return

    lines = "\n".join(f"• {a.get('message', '')}" for a in critical)
    subject = f"🔴 Critical alert: {client_name} — {period_label or 'latest report'}"

    if agency_notification_email:
        html = f"""
        <div style="font-family: sans-serif; max-width: 480px;">
          <h2 style="color:#DC2626;">Critical Action Item(s) Detected</h2>
          <p><strong>{client_name}</strong> — {period_label or 'Latest report'}</p>
          <ul>{''.join(f'<li>{a.get("message","")}</li>' for a in critical)}</ul>
          {f'<p><a href="{report_url}">View full report</a></p>' if report_url else ''}
          <p style="color:#9CA3AF; font-size:12px;">Sent automatically by RAKH.</p>
        </div>
        """
        _send_email(agency_notification_email, subject, html)

    if slack_webhook_url:
        slack_text = f"*{subject}*\n{lines}" + (f"\n<{report_url}|View report>" if report_url else "")
        _send_slack(slack_webhook_url, slack_text)


def notify_reminder(
    *,
    agency_notification_email: str | None,
    client_name: str,
) -> None:
    """Nudge the agency that a client's report cadence is due."""
    if not agency_notification_email:
        return
    subject = f"📅 Time to generate this month's report for {client_name}"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px;">
      <h2 style="color:#4F46E5;">Report Reminder</h2>
      <p>Based on {client_name}'s reporting cadence, it looks like a new report is due.
      Upload the latest campaign export in RAKH to generate it.</p>
      <p style="color:#9CA3AF; font-size:12px;">Sent automatically by RAKH.</p>
    </div>
    """
    _send_email(agency_notification_email, subject, html)


def notify_report_pending_review(
    *,
    agency_notification_email: str | None,
    client_name: str,
    period_label: str | None,
    review_url: str,
    auto_send_hours: int | None,
) -> None:
    """Sent the moment an auto-synced report is generated and waiting in
    the review queue. auto_send_hours is None if the client has auto-send
    disabled (report will wait for explicit manual approval instead)."""
    if not agency_notification_email:
        return

    if auto_send_hours is not None:
        timing_note = (
            f"It will automatically send to the client in {auto_send_hours} hours "
            f"unless you review and adjust it first."
        )
    else:
        timing_note = "It will stay in your review queue until you approve and send it manually."

    subject = f"📊 New auto-generated report ready: {client_name} — {period_label or ''}"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px;">
      <h2 style="color:#4F46E5;">Report Ready for Review</h2>
      <p>RAKH pulled fresh data for <strong>{client_name}</strong> and generated a new report.</p>
      <p>{timing_note}</p>
      <p><a href="{review_url}">Review it now</a></p>
      <p style="color:#9CA3AF; font-size:12px;">Sent automatically by RAKH.</p>
    </div>
    """
    _send_email(agency_notification_email, subject, html)


def send_report_to_client(
    *,
    client_email: str,
    client_name: str,
    agency_name: str,
    period_label: str | None,
    pdf_bytes: bytes,
    share_url: str,
) -> bool:
    """The actual client-facing delivery — attaches the PDF and links the
    live share page. This is the terminal step of the auto-send pipeline."""
    settings = get_settings()
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD):
        print("[notifications] SMTP not configured — skipping client report email.")
        return False

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"{client_name} Performance Report — {period_label or 'Latest Period'}"
    msg["From"] = settings.SMTP_FROM_EMAIL or settings.SMTP_USER
    msg["To"] = client_email

    body = MIMEText(
        f"""
        <div style="font-family: sans-serif; max-width: 480px;">
          <p>Hi {client_name} team,</p>
          <p>Your latest performance report is ready — see the attached PDF, or view it live:</p>
          <p><a href="{share_url}">{share_url}</a></p>
          <p style="color:#9CA3AF; font-size:12px;">Prepared by {agency_name} via RAKH.</p>
        </div>
        """,
        "html",
    )
    msg.attach(body)

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="report.pdf")
    msg.attach(attachment)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(msg["From"], [client_email], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notifications] Client report email failed: {exc}")
        return False
