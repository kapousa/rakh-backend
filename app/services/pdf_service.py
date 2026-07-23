"""
White-Label PDF Report Generator v3 (ReportLab + matplotlib).

v3 adds real Arabic support. ReportLab's built-in fonts (Helvetica, Times)
contain zero Arabic glyphs — passing Arabic text to them renders as solid
"missing glyph" boxes, not corrupted text, just literally no matching
character in the font. Fixing this needs three separate things, all
required together:
  1. A font that actually contains Arabic glyphs (Amiri, bundled in
     app/fonts/ — registered once at module load).
  2. Arabic text SHAPING — Arabic letters connect to their neighbors and
     change glyph form depending on position in a word (isolated/initial/
     medial/final). Raw Unicode codepoints are the *logical* letters, not
     the connected *presentation* forms — arabic_reshaper converts one to
     the other. Skipping this renders each letter disconnected and in its
     isolated form, which reads as garbled even with a correct font.
  3. BiDi reordering — Arabic is RTL, but ReportLab always draws strings
     left-to-right internally. python-bidi's get_display() converts the
     shaped logical string into left-to-right *visual* order so ReportLab
     draws it correctly (this is also what correctly keeps embedded
     numbers left-to-right within the RTL flow, matching how Arabic text
     with numerals actually reads).

This module also localizes the PDF's fixed chrome text (section headers,
KPI labels, footer) into Arabic when language='ar' — previously those
were hardcoded English regardless of the report's language setting, which
meant even a "fixed" Arabic report had English headers everywhere except
the AI-written paragraphs.
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from typing import Any

import arabic_reshaper
import httpx
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    Image as RLImage,
)

from app.services import chart_service

THEMES: dict[str, dict[str, str]] = {
    "corporate_blue": {"primary": "#1E40AF", "accent": "#3B82F6", "bg_light": "#EFF6FF"},
    "fresh_mint": {"primary": "#047857", "accent": "#10B981", "bg_light": "#ECFDF5"},
    "modern_minimalist": {"primary": "#18181B", "accent": "#71717A", "bg_light": "#F4F4F5"},
}

# ---------------------------------------------------------------------------
# Arabic font registration (once, at module import)
# ---------------------------------------------------------------------------
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "..", "fonts")
_ARABIC_FONT_AVAILABLE = False
try:
    pdfmetrics.registerFont(TTFont("Amiri", os.path.join(_FONTS_DIR, "Amiri-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Amiri-Bold", os.path.join(_FONTS_DIR, "Amiri-Bold.ttf")))
    _ARABIC_FONT_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 — degrade to Helvetica rather than crash PDF generation
    print(f"[pdf_service] Arabic font registration failed, Arabic PDFs will render incorrectly: {exc}")


def _shape_ar(text: str) -> str:
    """Reshape + BiDi-reorder Arabic text for correct ReportLab rendering.
    Safe to call on any string — a no-op on pure-Latin/numeric text."""
    if not text:
        return text
    return get_display(arabic_reshaper.reshape(text))


# ---------------------------------------------------------------------------
# Localized PDF chrome text — the fixed labels/headers around the AI content,
# which itself already arrives pre-translated from llm_service.py.
# ---------------------------------------------------------------------------
LABELS: dict[str, dict[str, str]] = {
    "impressions": {"en": "Impressions", "ar": "الظهور"},
    "clicks": {"en": "Clicks", "ar": "النقرات"},
    "ctr": {"en": "CTR", "ar": "نسبة النقر"},
    "spend": {"en": "Spend", "ar": "الإنفاق"},
    "conversions": {"en": "Conversions", "ar": "التحويلات"},
    "cpa": {"en": "CPA", "ar": "تكلفة الاكتساب"},
    "roas": {"en": "ROAS", "ar": "العائد على الإنفاق"},
    "revenue": {"en": "Revenue", "ar": "الإيرادات"},
    "performance_trends": {"en": "Performance Trends", "ar": "اتجاهات الأداء"},
    "daily_spend": {"en": "Daily Spend", "ar": "الإنفاق اليومي"},
    "daily_ctr": {"en": "Daily CTR", "ar": "نسبة النقر اليومية"},
    "mom_vs": {"en": "Month-over-Month vs.", "ar": "مقارنة شهرية مع"},
    "previous_period": {"en": "Previous Period", "ar": "الفترة السابقة"},
    "metric": {"en": "Metric", "ar": "المؤشر"},
    "previous": {"en": "Previous", "ar": "السابق"},
    "current": {"en": "Current", "ar": "الحالي"},
    "change": {"en": "Change", "ar": "التغيير"},
    "flagged_anomalies": {"en": "⚠ Flagged Anomalies", "ar": "⚠ المشاكل المرصودة"},
    "severity": {"en": "Severity", "ar": "الخطورة"},
    "details": {"en": "Details", "ar": "التفاصيل"},
    "analysis_insights": {"en": "Analysis & Insights", "ar": "التحليل والرؤى"},
    "recommendations": {"en": "Recommendations", "ar": "التوصيات"},
    "prepared_by": {"en": "Prepared by", "ar": "أُعد بواسطة"},
    "generated": {"en": "Generated", "ar": "تاريخ الإنشاء"},
    "critical_issues": {"en": "critical issue(s) require attention this period.", "ar": "مشكلة (مشاكل) حرجة تتطلب الانتباه خلال هذه الفترة."},
    "performance_improved": {"en": "Performance improved vs. last period — ROAS up", "ar": "تحسّن الأداء مقارنة بالفترة السابقة — ارتفع العائد على الإنفاق بنسبة"},
    "performance_softened": {"en": "Performance softened vs. last period — ROAS down", "ar": "تراجع الأداء مقارنة بالفترة السابقة — انخفض العائد على الإنفاق بنسبة"},
    "roas_delivered": {"en": "Account delivered a", "ar": "حقق الحساب عائدًا على الإنفاق قدره"},
    "roas_no_issues": {"en": "ROAS this period with no critical issues flagged.", "ar": "خلال هذه الفترة دون رصد أي مشاكل حرجة."},
}


def _l(key: str, language: str) -> str:
    """Look up a localized label, falling back to English if missing."""
    entry = LABELS.get(key, {})
    return entry.get(language, entry.get("en", key))


def _hex(c: str) -> colors.Color:
    return colors.HexColor(c)


def _fetch_logo_image(url: str | None, max_dim_mm: float = 22) -> RLImage | None:
    """Best-effort fetch + embed of a logo URL. Never raises — PDF export
    should not fail just because a logo link is stale or unreachable."""
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=6.0, follow_redirects=True)
        resp.raise_for_status()
        img = RLImage(io.BytesIO(resp.content))
        ratio = img.imageWidth / img.imageHeight if img.imageHeight else 1
        if ratio >= 1:
            img.drawWidth = max_dim_mm * mm
            img.drawHeight = (max_dim_mm / ratio) * mm
        else:
            img.drawHeight = max_dim_mm * mm
            img.drawWidth = (max_dim_mm * ratio) * mm
        return img
    except Exception as exc:  # noqa: BLE001 — logo fetch is non-critical
        print(f"[pdf_service] Logo fetch failed for {url}: {exc}")
        return None


def _kpi_table(metrics: dict[str, Any], theme: dict[str, str], language: str, font: str, font_bold: str) -> Table:
    data = [
        [_l("impressions", language), _l("clicks", language), _l("ctr", language), _l("spend", language)],
        [
            f"{metrics.get('impressions', 0):,}",
            f"{metrics.get('clicks', 0):,}",
            f"{metrics.get('ctr', 0)}%",
            f"${metrics.get('spend', 0):,.2f}",
        ],
        [_l("conversions", language), _l("cpa", language), _l("roas", language), _l("revenue", language)],
        [
            f"{metrics.get('conversions', 0)}",
            f"${metrics.get('cpa', 0):,.2f}",
            f"{metrics.get('roas', 0)}x",
            f"${metrics.get('revenue', 0):,.2f}",
        ],
    ]
    if language == "ar":
        data = [[_shape_ar(cell) for cell in row] for row in data]

    table = Table(data, colWidths=[42 * mm] * 4)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _hex(theme["primary"])),
                ("BACKGROUND", (0, 2), (-1, 2), _hex(theme["primary"])),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 2), (-1, 2), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), font_bold),
                ("FONTNAME", (0, 2), (-1, 2), font_bold),
                ("FONTNAME", (0, 1), (-1, 1), font),
                ("FONTNAME", (0, 3), (-1, 3), font),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS", (0, 1), (-1, 1), [_hex(theme["bg_light"])]),
                ("ROWBACKGROUNDS", (0, 3), (-1, 3), [_hex(theme["bg_light"])]),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ]
        )
    )
    return table


def _comparison_table(comparison: dict[str, Any], language: str, font: str, font_bold: str) -> Table | None:
    deltas = (comparison or {}).get("deltas")
    if not deltas:
        return None
    rows = [[_l("metric", language), _l("previous", language), _l("current", language), _l("change", language)]]
    metric_labels_key_map = {
        "impressions": "impressions", "clicks": "clicks", "ctr": "ctr", "spend": "spend",
        "conversions": "conversions", "cpa": "cpa", "cpc": "cpa", "roas": "roas", "revenue": "revenue",
    }
    for key, d in deltas.items():
        arrow = "▲" if d["pct_change"] >= 0 else "▼"
        label = _l(metric_labels_key_map.get(key, key), language) if language == "ar" else key.upper()
        rows.append([label, f"{d['before']:,}", f"{d['after']:,}", f"{arrow} {abs(d['pct_change'])}%"])

    if language == "ar":
        rows = [[_shape_ar(cell) for cell in row] for row in rows]

    table = Table(rows, colWidths=[35 * mm] * 4)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
    ]
    for i, (key, d) in enumerate(deltas.items(), start=1):
        color = colors.HexColor("#ECFDF5") if d["is_improvement"] else colors.HexColor("#FEF2F2")
        style.append(("BACKGROUND", (0, i), (-1, i), color))
    table.setStyle(TableStyle(style))
    return table


def _anomalies_table(anomalies: list[dict[str, Any]], language: str, font: str, font_bold: str) -> Table | None:
    if not anomalies:
        return None
    rows = [[_l("severity", language), _l("metric", language), _l("details", language)]]
    for a in anomalies:
        severity = a.get("severity", "").upper()
        metric = a.get("metric", "").upper()
        message = a.get("message", "")
        if language == "ar":
            message = _shape_ar(message)
        rows.append([severity, metric, message])
    table = Table(rows, colWidths=[22 * mm, 22 * mm, 120 * mm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT" if language == "ar" else "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
    ]
    for i, a in enumerate(anomalies, start=1):
        color = colors.HexColor("#FEE2E2") if a.get("severity") == "critical" else colors.HexColor("#FEF3C7")
        style.append(("BACKGROUND", (0, i), (-1, i), color))
    table.setStyle(TableStyle(style))
    return table


def _build_executive_summary(metrics: dict, anomalies: list[dict], comparison: dict, language: str) -> str:
    critical_count = sum(1 for a in anomalies if a.get("severity") == "critical")
    roas = metrics.get("roas", 0)

    if critical_count > 0:
        return f"⚠ {critical_count} {_l('critical_issues', language)}"
    if comparison.get("has_previous"):
        roas_delta = comparison.get("deltas", {}).get("roas", {})
        if roas_delta and roas_delta.get("is_improvement"):
            return f"✓ {_l('performance_improved', language)} {abs(roas_delta['pct_change'])}%."
        if roas_delta:
            return f"{_l('performance_softened', language)} {abs(roas_delta['pct_change'])}%."
    return f"{_l('roas_delivered', language)} {roas}x {_l('roas_no_issues', language)}"


def generate_pdf_report(
    *,
    agency_name: str,
    agency_logo_url: str | None,
    client_name: str,
    client_logo_url: str | None,
    platform: str,
    period_label: str | None,
    theme_key: str,
    metrics: dict[str, Any],
    daily_series: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    comparison: dict[str, Any] | None,
    ai_summary: str,
    ai_recommendations: list[str],
    language: str = "en",
) -> bytes:
    theme = THEMES.get(theme_key, THEMES["corporate_blue"])
    comparison = comparison or {}
    is_arabic = language == "ar" and _ARABIC_FONT_AVAILABLE
    font = "Amiri" if is_arabic else "Helvetica"
    font_bold = "Amiri-Bold" if is_arabic else "Helvetica-Bold"
    text_align = TA_RIGHT if is_arabic else TA_CENTER
    body_align = TA_RIGHT if is_arabic else None  # None = ReportLab default (left)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleBrand", parent=styles["Title"], textColor=_hex(theme["primary"]), fontSize=26,
        fontName=font_bold, alignment=text_align,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Normal"], fontSize=12, textColor=colors.HexColor("#6B7280"),
        alignment=text_align, fontName=font,
    )
    h2 = ParagraphStyle(
        "H2Brand", parent=styles["Heading2"], textColor=_hex(theme["primary"]),
        fontName=font_bold, alignment=text_align,
    )
    body_kwargs = {"fontName": font}
    if body_align:
        body_kwargs["alignment"] = body_align
    body = ParagraphStyle("BodyBrand", parent=styles["BodyText"], fontSize=10, leading=17, **body_kwargs)
    exec_style = ParagraphStyle(
        "ExecSummary", parent=styles["Normal"], fontSize=12.5, alignment=text_align,
        textColor=colors.HexColor("#111827"), spaceBefore=6, spaceAfter=6, fontName=font,
    )
    heading4 = ParagraphStyle("Heading4Brand", parent=styles["Heading4"], fontName=font_bold, alignment=text_align)
    footer_style = ParagraphStyle(
        "Footer", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#9CA3AF"),
        fontName=font, alignment=text_align,
    )

    def txt(s: str) -> str:
        """Shape Arabic text for display; no-op for English."""
        return _shape_ar(s) if is_arabic else s

    story: list[Any] = []

    # ============================ COVER PAGE ============================
    story.append(Spacer(1, 20 * mm))

    client_logo = _fetch_logo_image(client_logo_url, max_dim_mm=28)
    if client_logo:
        client_logo.hAlign = "CENTER"
        story.append(client_logo)
        story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(txt(client_name), title_style))
    period_text = f"{platform.title()} — {period_label or 'N/A'}" if is_arabic else f"{platform.title()} Performance Report — {period_label or 'N/A'}"
    story.append(Paragraph(txt(period_text), subtitle_style))
    story.append(Spacer(1, 14 * mm))

    exec_summary_text = _build_executive_summary(metrics, anomalies, comparison, language)
    exec_box = Table([[Paragraph(txt(exec_summary_text), exec_style)]], colWidths=[164 * mm])
    exec_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(theme["bg_light"])),
                ("BOX", (0, 0), (-1, -1), 0.75, _hex(theme["accent"])),
                ("TOPPADDING", (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )
    story.append(exec_box)
    story.append(Spacer(1, 14 * mm))
    story.append(_kpi_table(metrics, theme, language, font, font_bold))

    story.append(Spacer(1, 20 * mm))
    agency_logo = _fetch_logo_image(agency_logo_url, max_dim_mm=16)
    footer_cells = []
    if agency_logo:
        footer_cells.append(agency_logo)
    footer_text = Paragraph(
        txt(f"{_l('prepared_by', language)} {agency_name} &nbsp;|&nbsp; {_l('generated', language)} {datetime.utcnow().strftime('%Y-%m-%d')}"),
        footer_style,
    )
    footer_cells.append(footer_text)
    footer_table = Table([footer_cells], colWidths=[20 * mm, 144 * mm] if agency_logo else [164 * mm])
    footer_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(footer_table)

    story.append(PageBreak())

    # ============================ TRENDS ============================
    story.append(Paragraph(txt(_l("performance_trends", language)), h2))
    story.append(Spacer(1, 4 * mm))

    spend_chart = chart_service.render_spend_trend_chart(daily_series, theme["accent"])
    if spend_chart:
        story.append(Paragraph(txt(_l("daily_spend", language)), heading4))
        story.append(RLImage(io.BytesIO(spend_chart), width=164 * mm, height=164 * mm * (2.6 / 7.2)))
        story.append(Spacer(1, 6 * mm))

    ctr_chart = chart_service.render_ctr_trend_chart(daily_series, theme["accent"])
    if ctr_chart:
        story.append(Paragraph(txt(_l("daily_ctr", language)), heading4))
        story.append(RLImage(io.BytesIO(ctr_chart), width=164 * mm, height=164 * mm * (2.6 / 7.2)))
        story.append(Spacer(1, 8 * mm))

    # ============================ COMPARISON ============================
    if comparison.get("has_previous"):
        prev_label = comparison.get("previous_period_label") or _l("previous_period", language)
        story.append(Paragraph(txt(f"{_l('mom_vs', language)} {prev_label}"), h2))
        story.append(Spacer(1, 4 * mm))
        comp_chart = chart_service.render_comparison_bar_chart(comparison, theme["accent"])
        if comp_chart:
            story.append(RLImage(io.BytesIO(comp_chart), width=164 * mm, height=164 * mm * (2.8 / 7.2)))
            story.append(Spacer(1, 4 * mm))
        comp_table = _comparison_table(comparison, language, font, font_bold)
        if comp_table:
            story.append(comp_table)
            story.append(Spacer(1, 8 * mm))

    # ============================ ANOMALIES ============================
    anomaly_table = _anomalies_table(anomalies, language, font, font_bold)
    if anomaly_table:
        story.append(Paragraph(txt(_l("flagged_anomalies", language)), h2))
        story.append(Spacer(1, 4 * mm))
        story.append(anomaly_table)

    story.append(PageBreak())

    # ============================ AI NARRATIVE ============================
    story.append(Paragraph(txt(_l("analysis_insights", language)), h2))
    story.append(Spacer(1, 4 * mm))
    for para in ai_summary.split("\n\n"):
        if para.strip():
            story.append(Paragraph(txt(para.strip()), body))
            story.append(Spacer(1, 3 * mm))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(txt(_l("recommendations", language)), h2))
    story.append(Spacer(1, 3 * mm))
    for rec in ai_recommendations:
        prefix = "🔴 " if isinstance(rec, str) and rec.upper().startswith("CRITICAL") else "• "
        story.append(Paragraph(txt(f"{prefix}{rec}"), body))
        story.append(Spacer(1, 2 * mm))

    doc.build(story)
    return buffer.getvalue()
