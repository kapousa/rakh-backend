"""
Report Chat Assistant endpoints — v1, single-report scope.

ARCHITECTURE NOTE for v2 (cross-report Q&A): don't widen this same
"stuff everything into the prompt" approach — it works for one report's
bounded dataset, but a cross-report assistant should use real
tool-calling against agency-scoped DB queries instead, so numbers always
come from a real query result, never a model free-associating across a
large dumped context.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.db.supabase_client import get_current_agency_id, get_supabase
from app.models.schemas import ReportChatPreviewRequest, ReportChatRequest, ReportChatResponse
from app.services import report_chat_service
from app.services.feature_gate import agency_has_feature

router = APIRouter(tags=["report-chat"])


def _require_chat_feature(agency_id: str) -> None:
    if not agency_has_feature(agency_id, "ai_chat_assistant"):
        raise HTTPException(
            status_code=402,
            detail="The AI chat assistant is available on Pro plans and above. Upgrade to enable it.",
        )


@router.post("/api/reports/{report_id}/chat", response_model=ReportChatResponse)
async def chat_about_saved_report(
    report_id: str,
    payload: ReportChatRequest,
    agency_id: str = Depends(get_current_agency_id),
):
    _require_chat_feature(agency_id)

    sb = get_supabase()
    report_res = sb.table("reports").select("*").eq("id", report_id).eq("agency_id", agency_id).single().execute()
    if not report_res.data:
        raise HTTPException(status_code=404, detail="Report not found")
    report = report_res.data

    client_res = sb.table("clients").select("name").eq("id", report["client_id"]).single().execute()
    client_name = (client_res.data or {}).get("name", "Unknown client")

    report_context = {
        "client_name": client_name,
        "platform": report.get("platform"),
        "period_label": report.get("period_label"),
        "metrics": report.get("metrics", {}),
        "daily_series": report.get("daily_series", []),
        "anomalies": report.get("anomalies", []),
        "comparison": report.get("comparison", {}),
        "ai_summary": report.get("ai_summary"),
        "ai_recommendations": report.get("ai_recommendations", []),
    }

    history = [{"role": m.role, "content": m.content} for m in payload.conversation_history]
    reply = await report_chat_service.answer_question(report_context, history, payload.message)
    return ReportChatResponse(reply=reply)


@router.post("/api/report-chat/preview", response_model=ReportChatResponse)
async def chat_about_preview_report(
    payload: ReportChatPreviewRequest,
    agency_id: str = Depends(get_current_agency_id),
):
    """Same as above, but for a report still in the wizard (Step 4),
    not saved yet — the caller passes the report data directly."""
    _require_chat_feature(agency_id)

    report_context = {
        "client_name": payload.client_name,
        "platform": payload.platform,
        "period_label": payload.period_label,
        "metrics": payload.metrics,
        "daily_series": payload.daily_series,
        "anomalies": payload.anomalies,
        "comparison": payload.comparison,
        "ai_summary": payload.ai_summary,
        "ai_recommendations": payload.ai_recommendations,
    }

    history = [{"role": m.role, "content": m.content} for m in payload.conversation_history]
    reply = await report_chat_service.answer_question(report_context, history, payload.message)
    return ReportChatResponse(reply=reply)
