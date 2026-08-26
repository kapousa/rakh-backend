"""LLM Analysis Service.

Builds a consultant-grade prompt from parsed metrics + anomalies and
calls Groq or OpenAI. Output is forced into strict JSON matching the
reporting schema.
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
4. ACTIONABLE RECOMMENDATIONS: Provide specific, data-backed execution items. Each recommendation must include:
   - Finding: The specific numeric observation.
   - Tactical Action: The exact operational step (e.g., creative formats to introduce, ad sets to pause).
   - Expected Impact: The target KPI improvement.
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
    "period_comparison_summary": "string (explicit pre-drop vs. post-drop or MoM trend analysis)"
  },
  "root_cause_analysis": {
    "primary_issue": "string",
    "timeframe_identified": "string (e.g., performance drop starting Aug 16)",
    "detailed_diagnosis": "string"
  },
  "recommendations": [
    {
      "priority": "string (Critical / High / Medium)",
      "category": "string (e.g., Creative Strategy, Audience Targeting, Budget Allocation)",
      "finding": "string",
      "tactical_action": "string",
      "expected_impact": "string"
    }
  ]
}
"""

TONE_GUIDANCE = {
    "aggressive": (
        "Direct, urgent, and results-obsessed. Focus heavily on immediate"
        " corrective actions without softening bad news."
    ),
    "professional": (
        "Polished, consultant-grade, and balanced. Deliver authoritative,"
        " solution-oriented insights with a measured tone."
    ),
    "casual": (
        "Friendly, approachable, and plain-spoken—like a trusted growth"
        " advisor communicating directly with a peer."
    ),
}

LANGUAGE_GUIDANCE = {
    "en": (
        "Write entirely in English using clear, professional marketing"
        " terminology."
    ),
    "ar": (
        "Write entirely in professional Modern Standard Arabic (اللغة العربية"
        " الفصحى الاحترافية). Keep numbers in Western Arabic numerals (1, 2, 3)."
        " Include standard marketing acronyms in parentheses where helpful"
        " (e.g., ROAS, CTR, CPA)."
    ),
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

Write the JSON object matching the exact schema now.
""".strip()


def _call_groq(system: str, user: str, model: str, api_key: str) -> str:
  from groq import Groq

  client = Groq(api_key=api_key)
  resp = client.chat.completions.create(
      model=model,
      messages=[
          {"role": "system", "content": system},
          {"role": "user", "content": user},
      ],
      temperature=0.3,
      response_format={"type": "json_object"},
  )
  return resp.choices[0].message.content


def _call_openai(system: str, user: str, model: str, api_key: str) -> str:
  from openai import OpenAI

  client = OpenAI(
      base_url="https://api.groq.com/openai/v1"
      if "gsk_" in api_key
      else "https://api.openai.com/v1",
      api_key=api_key,
  )
  resp = client.chat.completions.create(
      model=model,
      messages=[
          {"role": "system", "content": system},
          {"role": "user", "content": user},
      ],
      temperature=0.3,
      response_format={"type": "json_object"},
  )
  return resp.choices[0].message.content


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
      # Default fallback to active Llama 3.3 model if GROQ_MODEL is misconfigured
      model_name = settings.GROQ_MODEL or "llama-3.3-70b-versatile"
      raw = _call_groq(SYSTEM_PROMPT, user_prompt, model_name, settings.GROQ_API_KEY)
    elif settings.OPENAI_API_KEY:
      model_name = settings.OPENAI_MODEL or "gpt-4o"
      raw = _call_openai(
          SYSTEM_PROMPT, user_prompt, model_name, settings.OPENAI_API_KEY
      )
  except Exception as exc:  # noqa: BLE001
    raw = None
    print(f"[llm_service] LLM call failed, falling back to template: {exc}")

  if raw:
    try:
      parsed = json.loads(raw)
      # Fix: Validate the NEW schema keys instead of old "summary" key
      if "executive_summary" in parsed or "report_title" in parsed:
        return parsed
    except json.JSONDecodeError:
      pass

  return _fallback_analysis(metrics, anomalies, client_name, language)


def _fallback_analysis(
    metrics: Metrics, anomalies: list[Anomaly], client_name: str, language: Language
) -> dict[str, Any]:
  """Updated deterministic fallback matching the new JSON schema."""
  if language == "ar":
    return {
        "report_title": f"تقرير أداء الحملات - {client_name}",
        "executive_summary": (
            f"خلال الفترة المشمولة بالتقرير، حقق حساب {client_name}"
            f" {metrics.impressions:,} ظهور و{metrics.clicks:,} نقرة بمعدل نقر"
            f" إلى ظهور {metrics.ctr}%.\n\nبلغ إجمالي الإنفاق"
            f" {metrics.spend:,.2f} دولار، بمتوسط تكلفة اكتساب قدره"
            f" {metrics.cpa:,.2f} دولار، وعائد إنفاق إعلاني قدره"
            f" {metrics.roas}x."
        ),
        "key_metrics_breakdown": {
            "spend": f"${metrics.spend:,.2f}",
            "revenue": f"${metrics.revenue:,.2f}",
            "roas": f"{metrics.roas}x",
            "cpa": f"${metrics.cpa:,.2f}",
            "ctr": f"{metrics.ctr}%",
            "conversions": str(metrics.conversions),
            "period_comparison_summary": "تحليل الأداء للفترة الحالية.",
        },
        "root_cause_analysis": {
            "primary_issue": "تراجع كفاءة الاستهداف الإعلاني",
            "timeframe_identified": "الفترة الحالية",
            "detailed_diagnosis": (
                "يتطلب الأمر مراجعة شاملة لجمهور الاستهداف والتصاميم"
                " الإعلانية."
            ),
        },
        "recommendations": [{
            "priority": "High",
            "category": "Audience Targeting",
            "finding": f"معدل النقر الحالي {metrics.ctr}%",
            "tactical_action": (
                "إعادة إطلاق حملات تستهدف جماهير مشابهة (Lookalike) جديدة."
            ),
            "expected_impact": "تحسين CTR بنسبة 20%",
        }],
    }

  return {
      "report_title": f"Performance Report - {client_name}",
      "executive_summary": (
          f"During the reporting period, {client_name}'s campaigns generated"
          f" {metrics.impressions:,} impressions and {metrics.clicks:,}"
          f" clicks, for a CTR of {metrics.ctr}%.\n\nTotal spend was"
          f" ${metrics.spend:,.2f}, resulting in a blended CPA of"
          f" ${metrics.cpa:,.2f} and a ROAS of {metrics.roas}x."
      ),
      "key_metrics_breakdown": {
          "spend": f"${metrics.spend:,.2f}",
          "revenue": f"${metrics.revenue:,.2f}",
          "roas": f"{metrics.roas}x",
          "cpa": f"${metrics.cpa:,.2f}",
          "ctr": f"{metrics.ctr}%",
          "conversions": str(metrics.conversions),
          "period_comparison_summary": "Current period aggregated metrics.",
      },
      "root_cause_analysis": {
          "primary_issue": "Creative Fatigue and Targeting Saturation",
          "timeframe_identified": "Reporting Period",
          "detailed_diagnosis": (
              "Current performance indicators reflect high ad frequency and"
              " audience saturation."
          ),
      },
      "recommendations": [{
          "priority": "High",
          "category": "Creative Strategy",
          "finding": f"Current CTR is {metrics.ctr}%",
          "tactical_action": (
              "Refresh primary visual assets and test short-form video formats."
          ),
          "expected_impact": "Increase CTR by 15-25%",
      }],
  }