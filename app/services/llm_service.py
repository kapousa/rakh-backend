"""
LLM Analysis Service.

Builds a consultant-grade prompt from parsed metrics + anomalies and
calls Groq or OpenAI. Normalizes output keys internally to preserve backwards
compatibility with all existing caller endpoints.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.models.schemas import Anomaly, Language, Metrics, Tone

SYSTEM_PROMPT = """You are Roasify AI, a principal performance marketing strategist writing comprehensive, consultant-grade monthly reports for agency clients.

Core Analysis Rules:
1. DATA RIGOR: Base every claim strictly on the provided numeric data. Never invent metrics or exaggerate figures.
2. TARGET SANITY CHECK: Validate benchmark/target anomalies before writing. If a target is realistically or mathematically impossible (e.g., target CTR > 20%, target ROAS > 100x), treat it as an internal setup error—do NOT report it to the client as a legitimate business failure.
3. ROOT-CAUSE DIAGNOSIS: Pinpoint the exact date or window where performance shifted (e.g., CTR collapse, CPA spike). Explain the underlying marketing drivers (e.g., ad creative fatigue, audience saturation, bidding shifts).
4. ACTIONABLE RECOMMENDATIONS: Provide specific, data-backed execution items.
5. JSON STRUCTURE: Output ONLY valid JSON matching the schema below. No markdown wrappers, no intro text.

JSON Schema:
{
  "report_title": "string",
  "executive_summary": "string (3-4 thorough paragraphs covering financial ROI, campaign highlights, and overarching strategic takeaway)",
  "key_metrics_breakdown": {
    "spend": "string",
    "revenue": "string",
    "roas": "string",
    "cpa": "string",
    "ctr": "string",
    "conversions": "string",
    "period_comparison_summary": "string"
  },
  "root_cause_analysis": {
    "primary_issue": "string",
    "timeframe_identified": "string",
    "detailed_diagnosis": "string"
  },
  "recommendations": [
    {
      "priority": "string",
      "category": "string",
      "finding": "string",
      "tactical_action": "string",
      "expected_impact": "string"
    }
  ]
}
"""

TONE_GUIDANCE = {
    "aggressive": "Direct, urgent, and results-obsessed. Focus heavily on immediate corrective actions without softening bad news.",
    "professional": "Polished, consultant-grade, and balanced. Deliver authoritative, solution-oriented insights with a measured tone.",
    "casual": "Friendly, approachable, and plain-spoken—like a trusted growth advisor communicating directly with a peer."
}

LANGUAGE_GUIDANCE = {
    "en": "Write entirely in English using clear, professional marketing terminology.",
    "ar": "Write entirely in professional Modern Standard Arabic (اللغة العربية الفصحى الاحترافية). Keep numbers in Western Arabic numerals (1, 2, 3). Include standard marketing acronyms in parentheses where helpful (e.g., ROAS, CTR, CPA)."
}


def _build_user_prompt(
    platform: str,
    period_label: str | None,
    metrics: Metrics,
    anomalies: list[Anomaly],
    tone: Tone,
    language: Language,
    client_name: str,
) -> str:
    anomalies_text = (
        "\n".join(f"- [{a.severity.upper()}] {a.message}" for a in anomalies)
        if anomalies
        else "None detected."
    )

    return f"""
Client: {client_name}
Ad platform: {platform}
Reporting period: {period_label or "Not specified"}

TONE: {tone} — {TONE_GUIDANCE[tone]}
LANGUAGE: {LANGUAGE_GUIDANCE[language]}

CAMPAIGN METRICS (aggregated for the period):
- Impressions: {metrics.impressions:,}
- Clicks: {metrics.clicks:,}
- CTR: {metrics.ctr}%
- Total Spend: ${metrics.spend:,.2f}
- Conversions: {metrics.conversions}
- CPA: ${metrics.cpa:,.2f}
- Revenue (if tracked): ${metrics.revenue:,.2f}
- ROAS: {metrics.roas}x

DETECTED ANOMALIES (statistically validated):
{anomalies_text}

Write the JSON object now.
""".strip()


def _call_groq(system: str, user: str, model: str, api_key: str) -> str:
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _call_openai(system: str, user: str, model: str, api_key: str) -> str:
    from openai import OpenAI

    client = OpenAI(
        base_url="https://api.groq.com/openai/v1" if "gsk_" in api_key else "https://api.openai.com/v1",
        api_key=api_key,
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _normalize_response(parsed: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizes structured LLM outputs back to legacy caller expectations
    without losing extended attributes.
    """
    # 1. Ensure 'summary' exists for old callers (e.g. analyze_upload)
    summary_text = parsed.get("summary") or parsed.get("executive_summary") or ""
    parsed["summary"] = summary_text

    # 2. Format recommendations into list of strings if they returned as structured objects
    raw_recs = parsed.get("recommendations", [])
    normalized_recs = []

    if isinstance(raw_recs, list):
        for rec in raw_recs:
            if isinstance(rec, dict):
                # Convert object rec to formatted string representation for backward compat
                tactical = rec.get("tactical_action") or rec.get("finding", "")
                impact = f" (Expected Impact: {rec['expected_impact']})" if rec.get("expected_impact") else ""
                priority = f"[{rec['priority']}] " if rec.get("priority") else ""
                normalized_recs.append(f"{priority}{tactical}{impact}".strip())
            elif isinstance(rec, str):
                normalized_recs.append(rec)

    parsed["recommendations"] = normalized_recs
    return parsed


async def generate_report_analysis(
    platform: str,
    period_label: str | None,
    metrics: Metrics,
    anomalies: list[Anomaly],
    tone: Tone,
    language: Language,
    client_name: str,
) -> dict[str, Any]:
    settings = get_settings()
    user_prompt = _build_user_prompt(
        platform, period_label, metrics, anomalies, tone, language, client_name
    )

    raw: str | None = None
    try:
        if settings.LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
            model_name = settings.GROQ_MODEL or "llama-3.3-70b-versatile"
            raw = _call_groq(SYSTEM_PROMPT, user_prompt, model_name, settings.GROQ_API_KEY)
        elif settings.OPENAI_API_KEY:
            model_name = settings.OPENAI_MODEL or "gpt-4o"
            raw = _call_openai(SYSTEM_PROMPT, user_prompt, model_name, settings.OPENAI_API_KEY)
    except Exception as exc:  # noqa: BLE001
        raw = None
        print(f"[llm_service] LLM call failed, falling back to template: {exc}")

    if raw:
        try:
            parsed = json.loads(raw)
            # Check for either old or new structure
            if "summary" in parsed or "executive_summary" in parsed or "report_title" in parsed:
                return _normalize_response(parsed)
        except json.JSONDecodeError:
            pass

    return _fallback_analysis(metrics, anomalies, client_name, language)


def _fallback_analysis(
    metrics: Metrics, anomalies: list[Anomaly], client_name: str, language: Language
) -> dict[str, Any]:
    """Deterministic, no-API fallback compatible with old and new schema fields."""
    if language == "ar":
        summary = (
            f"خلال الفترة المشمولة بالتقرير، حقق حساب {client_name} {metrics.impressions:,} ظهور "
            f"و{metrics.clicks:,} نقرة بمعدل نقر إلى ظهور {metrics.ctr}%.\n\n"
            f"بلغ إجمالي الإنفاق {metrics.spend:,.2f} دولار، بمتوسط تكلفة اكتساب قدره "
            f"{metrics.cpa:,.2f} دولار، وعائد إنفاق إعلاني قدره {metrics.roas}x."
        )
        recs = ["مراجعة استهداف الجمهور لتحسين معدل النقر.", "إعادة توزيع الميزانية نحو أفضل الحملات أداءً."]
    else:
        summary = (
            f"During the reporting period, {client_name}'s campaigns generated {metrics.impressions:,} "
            f"impressions and {metrics.clicks:,} clicks, for a CTR of {metrics.ctr}%.\n\n"
            f"Total spend was ${metrics.spend:,.2f}, resulting in a blended CPA of ${metrics.cpa:,.2f} "
            f"and a ROAS of {metrics.roas}x."
        )
        recs = [
            "Review audience targeting to improve engagement rate.",
            "Reallocate budget toward the strongest-performing campaigns.",
        ]

    if anomalies:
        critical = [a.message for a in anomalies if a.severity == "critical"]
        if critical:
            recs = [f"CRITICAL: {m}" for m in critical] + recs

    return {
        "summary": summary,
        "executive_summary": summary,
        "recommendations": recs,
        "report_title": f"Performance Report - {client_name}",
    }