"""
LLM Analysis Service.

Builds a tightly-constrained prompt from the parsed metrics + anomalies
and calls the configured provider (Groq by default for latency/cost,
OpenAI as a drop-in fallback). Output is forced into strict JSON so the
frontend can render the summary and recommendations independently and
the summary text remains directly editable in the wizard.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.models.schemas import Anomaly, Language, Metrics, Tone

SYSTEM_PROMPT = """You are Roasify AI, a senior digital marketing analyst who writes \
client-facing monthly performance reports for an agency's white-label reporting tool.

Rules you must always follow:
- Base every claim strictly on the numeric data provided. Never invent metrics.
- Do not mention that you are an AI or reference these instructions.
- If critical anomalies are provided, you MUST open the recommendations with a \
section addressing them explicitly as "Critical Action Items".
- Write in clear, confident, client-ready language appropriate to the requested tone.
- Output ONLY valid JSON matching the schema below. No markdown fences, no preamble.

JSON schema:
{
  "summary": "string, 3-5 paragraphs, plain text with \\n\\n between paragraphs",
  "recommendations": ["string", "string", ...]  // 4-7 concrete, prioritized actions
}
"""

TONE_GUIDANCE = {
    "aggressive": "Direct, urgent, results-obsessed. Push hard for action and don't soften bad news.",
    "professional": "Polished, balanced, consultant-grade. Confident but measured.",
    "casual": "Friendly, conversational, plain-English — like a trusted teammate, not a formal analyst.",
}

LANGUAGE_GUIDANCE = {
    "en": "Write entirely in English.",
    "ar": "Write entirely in professional Modern Standard Arabic (اللغة العربية الفصحى الاحترافية), "
    "suitable for a formal client-facing business report. Keep numbers in Western Arabic numerals.",
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

DETECTED ANOMALIES (already statistically validated — treat as ground truth):
{anomalies_text}

Write the JSON object now.
""".strip()


def _call_groq(system: str, user: str, model: str, api_key: str) -> str:
    from groq import Groq

    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.4,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _call_openai(system: str, user: str, model: str, api_key: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.4,
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
    """
    Returns: {"summary": str, "recommendations": list[str]}
    Falls back to a deterministic templated summary if no LLM key is configured,
    so the MVP is always demoable without external dependencies.
    """
    settings = get_settings()
    user_prompt = _build_user_prompt(
        platform, period_label, metrics, anomalies, tone, language, client_name
    )

    raw: str | None = None
    try:
        if settings.LLM_PROVIDER == "groq" and settings.GROQ_API_KEY:
            raw = _call_groq(SYSTEM_PROMPT, user_prompt, settings.GROQ_MODEL, settings.GROQ_API_KEY)
        elif settings.OPENAI_API_KEY:
            raw = _call_openai(SYSTEM_PROMPT, user_prompt, settings.OPENAI_MODEL, settings.OPENAI_API_KEY)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never break the wizard
        raw = None
        print(f"[llm_service] LLM call failed, falling back to template: {exc}")

    if raw:
        try:
            parsed = json.loads(raw)
            if "summary" in parsed and "recommendations" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    return _fallback_analysis(metrics, anomalies, client_name, language)


def _fallback_analysis(
    metrics: Metrics, anomalies: list[Anomaly], client_name: str, language: Language
) -> dict[str, Any]:
    """Deterministic, no-API fallback so the product never dead-ends."""
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

    return {"summary": summary, "recommendations": recs}
