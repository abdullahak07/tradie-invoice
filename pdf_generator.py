from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from models import Quote


def _field(obj: Any, name: str, default: str = "") -> str:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return str(obj.get(name, default) or default)
    return str(getattr(obj, name, default) or default)


def build_pdf(quote: Quote) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(quote.business_name, styles["Title"]),
        Paragraph(f"ABN: {quote.abn}", styles["Normal"]),
        Paragraph(f"Quote {quote.quote_number or 'DRAFT'} • Valid for 30 days", styles["Heading2"]),
        Spacer(1, 12),
    ]

    if quote.customer:
        story.append(Paragraph(f"Customer: {_field(quote.customer, 'name')}<br/>{_field(quote.customer, 'address')}", styles["Normal"]))

    rows = [["Description", "Qty", "Materials", "Hours", "Rate", "Line Total"]]
    for item in quote.line_items:
        line_total = item.quantity * item.material_unit_cost * (1 + item.material_markup_percent / 100) + item.labor_hours * item.hourly_rate
        rows.append([
            item.description,
            f"{item.quantity:g}",
            f"${item.material_unit_cost:.2f}",
            f"{item.labor_hours:.1f}",
            f"${item.hourly_rate:.2f}",
            f"${line_total:.2f}",
        ])
    rows += [
        ["", "", "", "", "Subtotal", f"${quote.subtotal:.2f}"],
        ["", "", "", "", "GST 10%", f"${quote.gst:.2f}"],
        ["", "", "", "", "Total", f"${quote.total:.2f}"],
    ]

    table = Table(rows, colWidths=[190, 35, 70, 55, 60, 70])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story += [Spacer(1, 12), table, Spacer(1, 18), Paragraph("Terms", styles["Heading3"]), Paragraph(quote.terms, styles["Normal"]), Spacer(1, 16), Paragraph("[ Accept Quote ]", styles["Heading2"])]
    document.build(story)
    return buffer.getvalue()
