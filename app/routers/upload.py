"""
Upload & Analyze pipeline.

POST /api/upload/analyze
  Accepts a CSV/XLSX file + platform + client_id, parses it, runs anomaly
  detection against the client's targets, calls the LLM for narrative
  analysis, and returns everything needed to render Step 4 of the wizard —
  WITHOUT yet persisting a report row (persistence happens on explicit save,
  see reports.py), so users can re-upload/retry freely.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.config import get_settings
from app.db.supabase_client import get_current_agency_id, get_supabase
from app.models.schemas import Language, Platform, Tone
from app.services.csv_parser import parse_campaign_file
from app.services.llm_service import generate_report_analysis

router = APIRouter(prefix="/api/upload", tags=["upload"])


@router.post("/analyze")
async def analyze_upload(
    file: UploadFile = File(...),
    platform: Platform = Form(...),
    client_id: str = Form(...),
    language: Language = Form("en"),
    tone: Tone = Form("professional"),
    period_label: str | None = Form(None),
    agency_id: str = Depends(get_current_agency_id),
):
    settings = get_settings()

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_SIZE_MB:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.MAX_UPLOAD_SIZE_MB}MB limit")

    # Fetch client to pull KPI targets + name for prompt personalization
    sb = get_supabase()
    client_res = (
        sb.table("clients").select("*").eq("id", client_id).eq("agency_id", agency_id).single().execute()
    )
    if not client_res.data:
        raise HTTPException(status_code=404, detail="Client not found")
    client = client_res.data

    try:
        parsed = parse_campaign_file(
            file_bytes=contents,
            filename=file.filename or "upload.csv",
            platform=platform,
            target_ctr=client.get("target_ctr"),
            target_cpa=client.get("target_cpa"),
            target_roas=client.get("target_roas"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    analysis = await generate_report_analysis(
        platform=platform,
        period_label=period_label,
        metrics=parsed.metrics,
        anomalies=parsed.anomalies,
        tone=tone,
        language=language,
        client_name=client["name"],
    )

    return {
        "platform": parsed.platform,
        "rows_parsed": parsed.rows_parsed,
        "metrics": parsed.metrics.model_dump(),
        "daily_series": parsed.daily_series,
        "anomalies": [a.model_dump() for a in parsed.anomalies],
        "ai_summary": analysis["summary"],
        "ai_recommendations": analysis["recommendations"],
    }
