"""
Ad platform integrations router.

OAuth flow:
  1. Frontend calls GET /connect/{platform}?client_id=... while the agency
     user is authenticated → returns an oauth_url to redirect the browser to.
  2. Agency completes consent on the platform's site, gets redirected back
     to GET /{platform}/callback?code=...&state=...
  3. Backend exchanges the code for tokens, lists available ad accounts,
     and returns them so the frontend can show an account picker.
  4. Frontend calls POST /connections/{id}/select-account to finalize which
     account maps to this client — only then does sync become active.

Review workflow: PATCH /reports/{id}/review lets an agency approve-and-send
early or hold a pending_review report, independent of the scheduler's
automatic send-after-window behavior (see sync_service.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.db.supabase_client import get_current_agency_id, get_supabase
from app.models.schemas import (
    AdAccountOut,
    ConnectionOut,
    ConnectionPreviewRequest,
    ConnectionUpdate,
    OAuthUrlOut,
    ReportOut,
    ReportReviewAction,
    SelectAccountRequest,
)
from app.services import sync_service
from app.services.feature_gate import agency_has_feature
from app.services.platform_connectors import get_connector
from app.services.platform_connectors.base import ConnectorNotAvailable, DateRange
from app.services.token_encryption import decrypt_token, encrypt_token

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/connect/{platform}", response_model=OAuthUrlOut)
async def start_connect(platform: str, client_id: str = Query(...), agency_id: str = Depends(get_current_agency_id)):
    if not agency_has_feature(agency_id, "connected_accounts"):
        raise HTTPException(
            status_code=402,
            detail="Connecting an ad account is available on Starter plans and above. Upgrade to enable it.",
        )

    sb = get_supabase()
    client_res = sb.table("clients").select("id").eq("id", client_id).eq("agency_id", agency_id).single().execute()
    if not client_res.data:
        raise HTTPException(status_code=404, detail="Client not found")

    try:
        connector = get_connector(platform)  # type: ignore[arg-type]
        # state encodes both the client we're connecting and the agency,
        # verified again on callback so a forged callback can't attach a
        # connection to the wrong agency.
        state = f"{agency_id}:{client_id}"
        url = connector.get_oauth_url(state)
    except ConnectorNotAvailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except RuntimeError as exc:  # missing config (client id/secret/dev token)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"oauth_url": url}


@router.get("/{platform}/callback")
async def oauth_callback(platform: str, code: str, state: str):
    """
    Public callback endpoint — the platform redirects the user's actual
    browser here (a top-level navigation, not a fetch from the SPA), so
    this must respond with a redirect back into the app rather than raw
    JSON, or the agency ends up staring at a blank JSON page mid-flow.

    Security relies on `state`, which we generated ourselves in
    start_connect and validate structurally + against the DB below.
    """
    settings = get_settings()
    frontend_error_url = f"{settings.FRONTEND_URL}/clients?connect_error=1"

    try:
        agency_id, client_id = state.split(":", 1)
    except ValueError:
        return RedirectResponse(url=frontend_error_url)

    sb = get_supabase()
    client_res = sb.table("clients").select("id").eq("id", client_id).eq("agency_id", agency_id).single().execute()
    if not client_res.data:
        return RedirectResponse(url=frontend_error_url)

    try:
        connector = get_connector(platform)  # type: ignore[arg-type]
        tokens = connector.exchange_code_for_tokens(code)
    except Exception:  # noqa: BLE001 — surface any failure as a clean redirect, not a crash mid-OAuth
        return RedirectResponse(url=frontend_error_url)

    expires_at = None
    if tokens.expires_in_seconds:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in_seconds)).isoformat()

    row = {
        "agency_id": agency_id,
        "client_id": client_id,
        "platform": platform,
        "access_token_encrypted": encrypt_token(tokens.access_token),
        "refresh_token_encrypted": encrypt_token(tokens.refresh_token) if tokens.refresh_token else None,
        "token_expires_at": expires_at,
        "sync_enabled": False,  # stays off until an account is explicitly selected
    }
    # upsert on (client_id, platform) — reconnecting replaces the old tokens
    existing = sb.table("ad_platform_connections").select("id").eq("client_id", client_id).eq("platform", platform).execute()
    if existing.data:
        sb.table("ad_platform_connections").update(row).eq("id", existing.data[0]["id"]).execute()
        connection_id = existing.data[0]["id"]
    else:
        res = sb.table("ad_platform_connections").insert(row).execute()
        connection_id = res.data[0]["id"]

    # Hand off to the frontend with just enough context to resume the flow —
    # the frontend calls GET /connections/{id}/accounts next (authenticated,
    # decrypts the token server-side) to render the account picker.
    return RedirectResponse(
        url=f"{settings.FRONTEND_URL}/clients?connected_platform={platform}&connection_id={connection_id}&client_id={client_id}"
    )


@router.get("/connections/{connection_id}/accounts", response_model=list[AdAccountOut])
async def get_connection_accounts(connection_id: str, agency_id: str = Depends(get_current_agency_id)):
    """
    Fetch the list of ad accounts this connection's token can access, for
    the account-picker step. Called by the frontend right after the OAuth
    redirect lands it back on /clients — separated from the callback itself
    so the callback can stay a simple redirect (see oauth_callback above).
    """
    sb = get_supabase()
    conn_res = (
        sb.table("ad_platform_connections")
        .select("*")
        .eq("id", connection_id)
        .eq("agency_id", agency_id)
        .single()
        .execute()
    )
    if not conn_res.data:
        raise HTTPException(status_code=404, detail="Connection not found")
    connection = conn_res.data

    try:
        connector = get_connector(connection["platform"])
        access_token = decrypt_token(connection["access_token_encrypted"])
        accounts = connector.list_ad_accounts(access_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to list ad accounts: {exc}") from exc

    return [AdAccountOut(external_id=a.external_id, name=a.name) for a in accounts]


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(client_id: str = Query(...), agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    res = (
        sb.table("ad_platform_connections")
        .select("*")
        .eq("client_id", client_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    return res.data


@router.post("/connections/{connection_id}/select-account", response_model=ConnectionOut)
async def select_account(
    connection_id: str,
    payload: SelectAccountRequest,
    agency_id: str = Depends(get_current_agency_id),
):
    sb = get_supabase()
    res = (
        sb.table("ad_platform_connections")
        .update({
            "external_account_id": payload.external_account_id,
            "external_account_name": payload.external_account_name,
            "sync_enabled": True,
        })
        .eq("id", connection_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Connection not found")
    return res.data[0]


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: str, payload: ConnectionUpdate, agency_id: str = Depends(get_current_agency_id)
):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    res = (
        sb.table("ad_platform_connections")
        .update(updates)
        .eq("id", connection_id)
        .eq("agency_id", agency_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Connection not found")
    return res.data[0]


@router.delete("/connections/{connection_id}", status_code=204)
async def disconnect(connection_id: str, agency_id: str = Depends(get_current_agency_id)):
    sb = get_supabase()
    sb.table("ad_platform_connections").delete().eq("id", connection_id).eq("agency_id", agency_id).execute()
    return None


@router.post("/connections/{connection_id}/sync-now", response_model=ReportOut)
async def sync_now(connection_id: str, agency_id: str = Depends(get_current_agency_id)):
    """Manually trigger an immediate background-style sync (still goes
    through the pending_review queue) instead of waiting for the schedule.
    For an on-demand pull with review happening right in the wizard instead,
    see POST /connections/{id}/preview below."""
    sb = get_supabase()
    conn_res = sb.table("ad_platform_connections").select("*").eq("id", connection_id).eq("agency_id", agency_id).single().execute()
    if not conn_res.data:
        raise HTTPException(status_code=404, detail="Connection not found")

    frequency_days = 7 if conn_res.data["sync_frequency"] == "weekly" else 30
    now = datetime.now(timezone.utc)
    date_range = DateRange(start=(now - timedelta(days=frequency_days)).date(), end=now.date())

    try:
        report = await sync_service.run_sync_for_connection(connection_id, date_range)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc

    return report


@router.post("/connections/{connection_id}/preview")
async def preview_connection_pull(
    connection_id: str,
    payload: ConnectionPreviewRequest,
    agency_id: str = Depends(get_current_agency_id),
):
    """
    Stateless pull-and-analyze for the Report Wizard's 'Pull from connected
    account' path — this is what makes connections hybrid rather than
    purely automatic. Returns the same shape as POST /api/upload/analyze
    (metrics, daily_series, anomalies, ai_summary, ai_recommendations) so
    the wizard's existing preview/edit UI (Step 4) works unchanged
    regardless of whether the data came from a CSV or a live pull. Nothing
    is saved here — the agency reviews/edits, then the normal
    POST /api/reports call persists it, tagged with source='auto_sync' and
    this connection_id for traceability.
    """
    try:
        result = await sync_service.preview_connection_pull(
            connection_id=connection_id,
            agency_id=agency_id,
            date_range=DateRange(start=payload.start_date, end=payload.end_date),
            language=payload.language,
            tone=payload.tone,
            period_label=payload.period_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to pull data from connected account: {exc}") from exc

    return result


@router.patch("/reports/{report_id}/review", response_model=ReportOut)
async def review_report(report_id: str, payload: ReportReviewAction, agency_id: str = Depends(get_current_agency_id)):
    """Let an agency approve-and-send early, or hold a pending_review report."""
    sb = get_supabase()
    report_res = sb.table("reports").select("*").eq("id", report_id).eq("agency_id", agency_id).single().execute()
    if not report_res.data:
        raise HTTPException(status_code=404, detail="Report not found")

    if payload.action == "hold":
        res = sb.table("reports").update({"review_status": "held"}).eq("id", report_id).execute()
        return res.data[0]

    # action == "approve" → send immediately, regardless of the review window
    updated_report = sync_service.send_report_now(report_id)
    return updated_report
