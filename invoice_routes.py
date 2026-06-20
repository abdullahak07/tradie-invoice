from __future__ import annotations

import base64
import json
import os
import re
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

import httpx
from PIL import Image, ImageChops
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib import colors
from db_backend import open_app_db, using_postgres

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "invoices"
DB_PATH = DATA_DIR / "message_invoices.db"

DATA_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Perth Tradie Services")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "")
BUSINESS_ABN = os.getenv("BUSINESS_ABN", "")
BUSINESS_ADDRESS = os.getenv("BUSINESS_ADDRESS", "")
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "")
BANK_BSB = os.getenv("BANK_BSB", "")
BANK_ACCOUNT_NUMBER = os.getenv("BANK_ACCOUNT_NUMBER", "")
PAYMENT_REFERENCE = os.getenv("PAYMENT_REFERENCE", "Invoice number")
BUSINESS_LOGO_PATH = os.getenv("BUSINESS_LOGO_PATH", "business_logo.png").strip()
DEFAULT_GST_RATE = float(os.getenv("DEFAULT_GST_RATE", "0.10"))


class MessageRequest(BaseModel):
    message: str = Field(min_length=5)


class InvoiceItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float = 0
    line_total: float = 0


class CustomerData(BaseModel):
    name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""


class InvoiceDraft(BaseModel):
    id: int
    invoice_number: str
    customer: CustomerData
    items: list[InvoiceItem]
    notes: str = ""
    due_date: str
    subtotal: float
    gst: float
    total: float
    gst_included: bool = False
    status: str
    created_at: str
    delivery: list[str] = []


def db():
    return open_app_db(DB_PATH)


def init_db() -> None:
    if using_postgres():
        return

    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT UNIQUE NOT NULL,
                source_message TEXT NOT NULL,
                customer_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL,
                subtotal REAL NOT NULL,
                gst REAL NOT NULL,
                total REAL NOT NULL,
                gst_included INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                delivery_json TEXT NOT NULL DEFAULT '[]',
                pdf_path TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                reminder_key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(invoice_id, reminder_key)
            );


            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_number TEXT UNIQUE NOT NULL,
                source_message TEXT NOT NULL,
                customer_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                expiry_date TEXT NOT NULL,
                subtotal REAL NOT NULL,
                gst REAL NOT NULL,
                total REAL NOT NULL,
                gst_included INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                converted_invoice_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_quotes_status
            ON quotes(status);

            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                name_key TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                phone_key TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                email_key TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_customers_name_key
            ON customers(name_key);

            CREATE INDEX IF NOT EXISTS idx_customers_phone_key
            ON customers(phone_key);

            CREATE INDEX IF NOT EXISTS idx_customers_email_key
            ON customers(email_key);
            """
        )


init_db()


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def normalise_phone(value: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", value)
    if cleaned.startswith("+61"):
        return "0" + cleaned[3:]
    if cleaned.startswith("61") and len(cleaned) >= 11:
        return "0" + cleaned[2:]
    return cleaned


def parse_money(value: str) -> float:
    return float(value.replace(",", "").strip())


def title_case_name(value: str) -> str:
    return " ".join(part.capitalize() for part in value.strip().split())


def extract_customer(text: str) -> CustomerData:
    lines = [re.sub(r"\s+", " ", line).strip(" ,.;") for line in text.splitlines()]
    lines = [line for line in lines if line]

    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    phone_match = re.search(r"(?:\+?61|0)[\s-]?\d(?:[\s-]?\d){7,9}", text)

    name = ""
    labelled_name = re.search(
        r"(?:customer\s+name|client\s+name|customer|client|name)"
        r"\s*(?:is|:|-)?\s*([A-Za-z][A-Za-z .'-]{1,60}?)"
        r"(?=\s+(?:phone|mobile|email|address)\b|[,;\n]|$)",
        text,
        re.I,
    )
    if labelled_name:
        name = title_case_name(labelled_name.group(1))
    else:
        for line in lines:
            if (
                re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}", line)
                and 1 <= len(line.split()) <= 5
                and not re.search(
                    r"\b(?:due|invoice|installed|install|replaced|replace|callout|"
                    r"call-out|wire|light|bulb|point|fan|service|labour|labor|wa)\b",
                    line,
                    re.I,
                )
            ):
                name = title_case_name(line)
                break

    address = ""
    labelled_address = re.search(
        r"(?:job\s+address|customer\s+address|address|job\s+at|located\s+at)"
        r"\s*(?:is|:|-)?\s*(.+?)"
        r"(?=\s+(?:phone|mobile|email|due|installed|replaced|supplied|"
        r"labour|labor|callout|call-out)\b|[;\n]|$)",
        text,
        re.I,
    )
    if labelled_address:
        address = labelled_address.group(1).strip(" ,.")
    else:
        excluded = {name.lower()} if name else set()
        if email_match:
            excluded.add(email_match.group(0).lower())
        if phone_match:
            excluded.add(re.sub(r"\s+", "", phone_match.group(0)).lower())

        for line in lines:
            compact = re.sub(r"\s+", "", line).lower()
            if line.lower() in excluded or compact in excluded:
                continue
            if "@" in line or "$" in line:
                continue
            if re.search(r"^\s*(?:due|payment\s+due)\b", line, re.I):
                continue
            if re.search(r"\b(?:installed|install|replaced|replace|callout|call-out)\b", line, re.I):
                continue
            if re.search(r"\bWA\b", line, re.I) or re.search(
                r"\b(?:street|st|road|rd|avenue|ave|drive|dr|court|ct|way|"
                r"crescent|cres|lane|ln|place|pl|terrace|tce)\b",
                line,
                re.I,
            ):
                address = line
                break

    return CustomerData(
        name=name,
        phone=normalise_phone(phone_match.group(0)) if phone_match else "",
        email=email_match.group(0) if email_match else "",
        address=address,
    )

def clean_description(value: str) -> str:
    value = re.sub(r"\b(?:installed|install|replaced|replace|supplied|supply|completed|did)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.-")
    return value[:120] or "Service item"


def parse_items(text: str) -> list[InvoiceItem]:
    items: list[InvoiceItem] = []

    money = r"(?:\$\s*(?P<price_a>\d[\d,]*(?:\.\d{1,2})?)|(?P<price_b>\d[\d,]*(?:\.\d{1,2})?)\s*\$)"
    qty_word = r"(?P<qty>\d+(?:\.\d+)?)"
    unit = r"(?P<unit>m|metres?|meters?|hrs?|hours?|kg|items?|units?)?"

    each_pattern = re.compile(
        rf"^{qty_word}\s*{unit}\s*"
        rf"(?P<desc>[A-Za-z][A-Za-z0-9 /&+\-]*?)\s+"
        rf"(?:at|@|for)\s*{money}"
        rf"(?:\s*(?:each|ea|per)\s*(?P<per_qty>\d+(?:\.\d+)?)?\s*(?P<per_unit>m|metres?|meters?|hrs?|hours?|kg|items?|units?)?)?\s*$",
        re.I,
    )

    simple_pattern = re.compile(
        rf"^(?P<desc>[A-Za-z][A-Za-z0-9 /&+\-]*?)\s+"
        rf"(?:at|@|for)?\s*{money}\s*$",
        re.I,
    )

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" ,.;")
        if not line:
            continue
        if re.search(r"@", line) and re.search(r"\.[A-Za-z]{2,}", line):
            continue
        if re.fullmatch(r"(?:\+?61|0)[\s-]?\d(?:[\s-]?\d){7,9}", line):
            continue
        if re.search(r"^\s*(?:due|payment\s+due)\b", line, re.I):
            continue
        if "$" not in line:
            continue

        match = each_pattern.match(line)
        if match:
            qty = float(match.group("qty"))
            price_text = match.group("price_a") or match.group("price_b")
            price = parse_money(price_text)
            desc = clean_description(match.group("desc"))
            item_unit = (match.group("unit") or "").lower()
            per_qty = float(match.group("per_qty")) if match.group("per_qty") else None
            per_unit = (match.group("per_unit") or "").lower()

            if item_unit and per_qty and per_unit:
                unit_price = round(price / per_qty, 4)
                display_unit = "metre" if item_unit.startswith("m") else item_unit.rstrip("s")
                description = f"{desc} ({display_unit})"
                line_total = round(qty * unit_price, 2)
            else:
                unit_price = price
                description = desc
                line_total = round(qty * unit_price, 2)

            items.append(
                InvoiceItem(
                    description=description,
                    quantity=qty,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )
            continue

        match = simple_pattern.match(line)
        if match:
            desc = clean_description(match.group("desc"))
            blocked = ("subtotal", "gst", "total", "invoice", "phone", "email")
            if any(word in desc.lower() for word in blocked):
                continue

            price_text = match.group("price_a") or match.group("price_b")
            amount = parse_money(price_text)
            items.append(
                InvoiceItem(
                    description=desc,
                    quantity=1,
                    unit_price=amount,
                    line_total=amount,
                )
            )

    return items

def parse_due_date(text: str) -> date:
    days_match = re.search(r"(?:due|payment\s+due)\s+(?:in\s+)?(\d+)\s+days?", text, re.I)
    if days_match:
        return date.today() + timedelta(days=int(days_match.group(1)))

    date_match = re.search(r"(?:due|payment\s+due)\s+(?:on\s+)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text, re.I)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year_text = date_match.group(3)
        year = int(year_text) if year_text else date.today().year
        if year < 100:
            year += 2000
        return date(year, month, day)

    return date.today() + timedelta(days=7)


def parse_message(text: str) -> dict[str, Any]:
    customer = extract_customer(text)
    items = parse_items(text)

    if not items:
        raise HTTPException(
            status_code=422,
            detail="No priced invoice items were found. Include amounts such as '4 downlights at $95 each' or 'Callout $120'.",
        )

    gst_included = bool(re.search(r"(?:including|incl\.?|includes)\s+gst", text, re.I))
    raw_total = round(sum(item.line_total for item in items), 2)

    if gst_included:
        total = raw_total
        subtotal = round(total / (1 + DEFAULT_GST_RATE), 2)
        gst = round(total - subtotal, 2)
    else:
        subtotal = raw_total
        gst = round(subtotal * DEFAULT_GST_RATE, 2)
        total = round(subtotal + gst, 2)

    delivery = []
    if customer.email:
        delivery.append("email")
    if customer.phone:
        delivery.append("sms")

    notes_match = re.search(r"(?:notes?|description)\s*[:\-]\s*(.+?)(?:\n|$)", text, re.I)
    notes = notes_match.group(1).strip() if notes_match else ""

    return {
        "customer": customer,
        "items": items,
        "notes": notes,
        "due_date": parse_due_date(text),
        "subtotal": subtotal,
        "gst": gst,
        "total": total,
        "gst_included": gst_included,
        "delivery": delivery,
    }


def row_to_invoice(row: sqlite3.Row) -> InvoiceDraft:
    return InvoiceDraft(
        id=row["id"],
        invoice_number=row["invoice_number"],
        customer=CustomerData(**json.loads(row["customer_json"])),
        items=[InvoiceItem(**item) for item in json.loads(row["items_json"])],
        notes=row["notes"],
        due_date=row["due_date"],
        subtotal=row["subtotal"],
        gst=row["gst"],
        total=row["total"],
        gst_included=bool(row["gst_included"]),
        status=row["status"],
        created_at=row["created_at"],
        delivery=json.loads(row["delivery_json"]),
    )


def get_invoice(invoice_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return row


def resolve_business_logo_path() -> Path | None:
    if not BUSINESS_LOGO_PATH:
        return None

    candidate = Path(BUSINESS_LOGO_PATH).expanduser()
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate

    if not candidate.is_file():
        return None

    if candidate.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        print("Business logo skipped: only PNG and JPEG files are supported")
        return None

    return candidate


def prepare_business_logo() -> Path | None:
    logo_path = resolve_business_logo_path()
    if logo_path is None:
        return None

    try:
        with Image.open(logo_path) as source:
            image = source.convert("RGBA")

            alpha_box = image.getchannel("A").getbbox()
            white = Image.new("RGBA", image.size, (255, 255, 255, 255))
            difference = ImageChops.difference(image, white)
            content_box = difference.getbbox()

            crop_box = content_box or alpha_box
            if crop_box:
                left, top, right, bottom = crop_box
                padding = max(6, int(max(right - left, bottom - top) * 0.04))
                left = max(0, left - padding)
                top = max(0, top - padding)
                right = min(image.width, right + padding)
                bottom = min(image.height, bottom + padding)
                image = image.crop((left, top, right, bottom))

            output = DATA_DIR / "_business_logo_prepared.png"
            image.save(output, "PNG")
            return output

    except Exception as exc:
        print(f"Business logo preparation skipped: {exc}")
        return logo_path


def create_business_logo():
    logo_path = prepare_business_logo()
    if logo_path is None:
        return None

    try:
        image_reader = ImageReader(str(logo_path))
        width_px, height_px = image_reader.getSize()

        if width_px <= 0 or height_px <= 0:
            raise ValueError("Logo has invalid dimensions")

        max_width = 68 * mm
        max_height = 30 * mm
        scale = min(
            max_width / float(width_px),
            max_height / float(height_px),
        )

        logo = RLImage(
            str(logo_path),
            width=width_px * scale,
            height=height_px * scale,
        )
        logo.hAlign = "LEFT"
        return logo

    except Exception as exc:
        print(f"Business logo skipped: {exc}")
        return None


def create_pdf(row: sqlite3.Row) -> Path:
    invoice = row_to_invoice(row)
    output = PDF_DIR / f"{invoice.invoice_number}.pdf"

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    story = []
    logo = create_business_logo()
    if logo is not None:
        story.extend([logo, Spacer(1, 3 * mm)])

    story.extend([
        Paragraph(BUSINESS_NAME, styles["Title"]),
        Paragraph("TAX INVOICE", styles["Heading2"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>Invoice:</b> {invoice.invoice_number}", styles["BodyText"]),
        Paragraph(f"<b>Date:</b> {invoice.created_at[:10]}", styles["BodyText"]),
        Paragraph(f"<b>Due:</b> {invoice.due_date}", styles["BodyText"]),
        Spacer(1, 5 * mm),
        Paragraph(f"<b>Bill to:</b> {invoice.customer.name or 'Customer'}", styles["BodyText"]),
        Paragraph(invoice.customer.address or "", styles["BodyText"]),
        Paragraph(invoice.customer.email or "", styles["BodyText"]),
        Paragraph(invoice.customer.phone or "", styles["BodyText"]),
        Spacer(1, 6 * mm),
    ])

    table_data = [["Description", "Qty", "Unit price", "Amount"]]
    for item in invoice.items:
        table_data.append([
            item.description,
            f"{item.quantity:g}",
            f"${item.unit_price:,.2f}",
            f"${item.line_total:,.2f}",
        ])

    table_data.extend([
        ["", "", "Subtotal", f"${invoice.subtotal:,.2f}"],
        ["", "", "GST", f"${invoice.gst:,.2f}"],
        ["", "", "Total", f"${invoice.total:,.2f}"],
    ])

    table = Table(table_data, colWidths=[92 * mm, 18 * mm, 30 * mm, 32 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -4), 0.4, colors.HexColor("#d1d5db")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (2, -1), (-1, -1), colors.HexColor("#ecfdf3")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(table)

    if invoice.notes:
        story.extend([
            Spacer(1, 6 * mm),
            Paragraph(f"<b>Notes:</b> {invoice.notes}", styles["BodyText"]),
        ])

    payment_lines = []
    if BANK_ACCOUNT_NAME:
        payment_lines.append(f"<b>Account name:</b> {BANK_ACCOUNT_NAME}")
    if BANK_BSB:
        payment_lines.append(f"<b>BSB:</b> {BANK_BSB}")
    if BANK_ACCOUNT_NUMBER:
        payment_lines.append(f"<b>Account number:</b> {BANK_ACCOUNT_NUMBER}")
    if PAYMENT_REFERENCE:
        reference = (
            invoice.invoice_number
            if PAYMENT_REFERENCE.lower() == "invoice number"
            else PAYMENT_REFERENCE
        )
        payment_lines.append(f"<b>Payment reference:</b> {reference}")

    if payment_lines:
        story.extend([
            Spacer(1, 7 * mm),
            Paragraph("<b>Payment details</b>", styles["Heading3"]),
            *[Paragraph(line, styles["BodyText"]) for line in payment_lines],
        ])

    business_bits = [
        x
        for x in [
            BUSINESS_ABN and f"ABN {BUSINESS_ABN}",
            BUSINESS_ADDRESS,
            BUSINESS_PHONE,
        ]
        if x
    ]
    if business_bits:
        story.extend([
            Spacer(1, 8 * mm),
            Paragraph(" | ".join(business_bits), styles["BodyText"]),
        ])

    doc.build(story)

    with db() as conn:
        conn.execute("UPDATE invoices SET pdf_path = ? WHERE id = ?", (str(output), invoice.id))

    return output



QUOTE_PDF_DIR = DATA_DIR / "quotes"
QUOTE_PDF_DIR.mkdir(parents=True, exist_ok=True)


def create_quote_pdf(quote) -> Path:
    output = QUOTE_PDF_DIR / f"{quote.quote_number}.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    story = []
    logo = create_business_logo()
    if logo is not None:
        story.extend([logo, Spacer(1, 3 * mm)])

    story.extend([
        Paragraph(BUSINESS_NAME, styles["Title"]),
        Paragraph("QUOTE", styles["Heading2"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>Quote:</b> {quote.quote_number}", styles["BodyText"]),
        Paragraph(f"<b>Date:</b> {quote.created_at[:10]}", styles["BodyText"]),
        Paragraph(
            f"<b>Valid until:</b> {quote.expiry_date}",
            styles["BodyText"],
        ),
        Spacer(1, 5 * mm),
        Paragraph(
            f"<b>Prepared for:</b> {quote.customer.name or 'Customer'}",
            styles["BodyText"],
        ),
        Paragraph(quote.customer.address or "", styles["BodyText"]),
        Paragraph(quote.customer.email or "", styles["BodyText"]),
        Paragraph(quote.customer.phone or "", styles["BodyText"]),
        Spacer(1, 6 * mm),
    ])

    table_data = [["Description", "Qty", "Unit price", "Amount"]]
    for item in quote.items:
        table_data.append([
            item.description,
            f"{item.quantity:g}",
            f"${item.unit_price:,.2f}",
            f"${item.line_total:,.2f}",
        ])

    table_data.extend([
        ["", "", "Subtotal", f"${quote.subtotal:,.2f}"],
        ["", "", "GST", f"${quote.gst:,.2f}"],
        ["", "", "Total", f"${quote.total:,.2f}"],
    ])

    table = Table(
        table_data,
        colWidths=[92 * mm, 18 * mm, 30 * mm, 32 * mm],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -4), 0.4, colors.HexColor("#d1d5db")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (2, -1), (-1, -1), colors.HexColor("#eff6ff")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(table)

    if quote.notes:
        story.extend([
            Spacer(1, 6 * mm),
            Paragraph(
                f"<b>Notes:</b> {quote.notes}",
                styles["BodyText"],
            ),
        ])

    business_bits = [
        value
        for value in [
            BUSINESS_ABN and f"ABN {BUSINESS_ABN}",
            BUSINESS_ADDRESS,
            BUSINESS_PHONE,
        ]
        if value
    ]
    if business_bits:
        story.extend([
            Spacer(1, 8 * mm),
            Paragraph(" | ".join(business_bits), styles["BodyText"]),
        ])

    story.extend([
        Spacer(1, 6 * mm),
        Paragraph(
            "This quote is valid until the date shown above and may be "
            "subject to change if the scope of work changes.",
            styles["BodyText"],
        ),
    ])

    doc.build(story)
    return output


def send_quote_email(quote, pdf_path: Path) -> tuple[bool, str]:
    sender = os.getenv(
        "SMTP_FROM",
        os.getenv("BUSINESS_EMAIL", ""),
    ).strip()
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()

    if not quote.customer.email:
        return False, "Customer email missing"
    if not brevo_api_key:
        return False, "BREVO_API_KEY is not configured"
    if not sender:
        return False, "SMTP_FROM or BUSINESS_EMAIL is not configured"

    email_body = (
        f"Hi {quote.customer.name or 'there'},\n\n"
        f"Please find attached quote {quote.quote_number} "
        f"for ${quote.total:,.2f}.\n"
        f"This quote is valid until {quote.expiry_date}.\n\n"
        f"Please reply to this email if you would like to proceed "
        f"or need any changes.\n\n"
        f"Regards,\n{BUSINESS_NAME}"
    )

    payload = {
        "sender": {
            "name": BUSINESS_NAME,
            "email": sender,
        },
        "to": [
            {
                "email": quote.customer.email,
                "name": quote.customer.name or "Customer",
            }
        ],
        "subject": f"Quote {quote.quote_number} from {BUSINESS_NAME}",
        "textContent": email_body,
        "attachment": [
            {
                "name": pdf_path.name,
                "content": base64.b64encode(
                    pdf_path.read_bytes()
                ).decode("ascii"),
            }
        ],
    }

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            return False, (
                f"Brevo API failed with status "
                f"{response.status_code}: {response.text[:250]}"
            )
        return True, "Quote email sent through Brevo API"
    except Exception as exc:
        return False, f"Brevo API error: {str(exc)[:250]}"


def send_email(invoice: InvoiceDraft, pdf_path: Path) -> tuple[bool, str]:
    sender = os.getenv(
        "SMTP_FROM",
        os.getenv("BUSINESS_EMAIL", "")
    ).strip()

    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()

    if not invoice.customer.email:
        return False, "Customer email missing"

    if not brevo_api_key:
        return False, "BREVO_API_KEY is not configured"

    if not sender:
        return False, "SMTP_FROM or BUSINESS_EMAIL is not configured"

    payment_email_lines = []
    if BANK_ACCOUNT_NAME:
        payment_email_lines.append(f"Account name: {BANK_ACCOUNT_NAME}")
    if BANK_BSB:
        payment_email_lines.append(f"BSB: {BANK_BSB}")
    if BANK_ACCOUNT_NUMBER:
        payment_email_lines.append(f"Account number: {BANK_ACCOUNT_NUMBER}")
    if PAYMENT_REFERENCE:
        reference = (
            invoice.invoice_number
            if PAYMENT_REFERENCE.lower() == "invoice number"
            else PAYMENT_REFERENCE
        )
        payment_email_lines.append(f"Payment reference: {reference}")

    payment_email = ""
    if payment_email_lines:
        payment_email = (
            "\nPayment details:\n"
            + "\n".join(payment_email_lines)
            + "\n"
        )

    email_body = (
        f"Hi {invoice.customer.name or 'there'},\n\n"
        f"Please find attached invoice {invoice.invoice_number} "
        f"for ${invoice.total:,.2f}.\n"
        f"Payment is due on {invoice.due_date}.\n"
        f"{payment_email}\n"
        f"Regards,\n{BUSINESS_NAME}"
    )

    payload = {
        "sender": {
            "name": BUSINESS_NAME,
            "email": sender,
        },
        "to": [
            {
                "email": invoice.customer.email,
                "name": invoice.customer.name or "Customer",
            }
        ],
        "subject": f"Invoice {invoice.invoice_number} from {BUSINESS_NAME}",
        "textContent": email_body,
        "attachment": [
            {
                "name": pdf_path.name,
                "content": base64.b64encode(
                    pdf_path.read_bytes()
                ).decode("ascii"),
            }
        ],
    }

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": brevo_api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        if response.status_code >= 400:
            return False, (
                f"Brevo API failed with status "
                f"{response.status_code}: {response.text[:250]}"
            )

        return True, "Email sent through Brevo API"

    except Exception as exc:
        return False, f"Brevo API error: {str(exc)[:250]}"


async def send_sms(phone: str, message: str) -> tuple[bool, str]:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")

    if not phone:
        return False, "Customer phone missing"
    if not all([sid, token, from_number]):
        return False, "Twilio not configured"

    to_number = phone
    if phone.startswith("0"):
        to_number = "+61" + phone[1:]

    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            headers={"Authorization": f"Basic {auth}"},
            data={"From": from_number, "To": to_number, "Body": message},
        )
        if response.status_code >= 400:
            return False, f"SMS failed: {response.text[:180]}"

    return True, "SMS sent"


@router.post("/invoice-drafts", response_model=InvoiceDraft)
def create_invoice_draft(payload: MessageRequest) -> InvoiceDraft:
    parsed = parse_message(payload.message)
    now = datetime.now().isoformat(timespec="seconds")

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, source_message, customer_json, items_json, notes,
                due_date, subtotal, gst, total, gst_included, status,
                delivery_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (
                "PENDING",
                payload.message,
                parsed["customer"].model_dump_json(),
                json.dumps([item.model_dump() for item in parsed["items"]]),
                parsed["notes"],
                parsed["due_date"].isoformat(),
                parsed["subtotal"],
                parsed["gst"],
                parsed["total"],
                int(parsed["gst_included"]),
                json.dumps(parsed["delivery"]),
                now,
            ),
        )
        invoice_id = cursor.lastrowid
        invoice_number = f"INV-{date.today():%Y%m%d}-{invoice_id:04d}"
        conn.execute(
            "UPDATE invoices SET invoice_number = ? WHERE id = ?",
            (invoice_number, invoice_id),
        )
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()

    return row_to_invoice(row)


@router.get("/invoices", response_model=list[InvoiceDraft])
def list_invoices() -> list[InvoiceDraft]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM invoices ORDER BY id DESC LIMIT 100").fetchall()
    return [row_to_invoice(row) for row in rows]


@router.get("/invoices/{invoice_id}", response_model=InvoiceDraft)
def read_invoice(invoice_id: int) -> InvoiceDraft:
    return row_to_invoice(get_invoice(invoice_id))


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: int) -> FileResponse:
    row = get_invoice(invoice_id)

    stored_path = row["pdf_path"]
    path = Path(stored_path) if stored_path else None

    # Railway local files can disappear after a restart or redeploy.
    # Rebuild the PDF from the persistent invoice data whenever missing.
    if path is None or not path.is_file():
        path = create_pdf(row)

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
    )


@router.post("/invoices/{invoice_id}/send")
async def approve_and_send(invoice_id: int, request: Request) -> dict[str, Any]:
    row = get_invoice(invoice_id)
    invoice = row_to_invoice(row)
    pdf_path = create_pdf(row)

    public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        public_base = str(request.base_url).rstrip("/")
    pdf_url = f"{public_base}/invoices/{invoice_id}/pdf"

    email_ok, email_result = send_email(invoice, pdf_path)
    sms_text = (
        f"Hi {invoice.customer.name or 'there'}, invoice {invoice.invoice_number} "
        f"from {BUSINESS_NAME} is ${invoice.total:,.2f}, due {invoice.due_date}. "
        f"View: {pdf_url}"
    )
    sms_ok, sms_result = await send_sms(invoice.customer.phone, sms_text)

    # In demo mode, preserve the invoice and clearly report that delivery is simulated.
    actual_delivery = email_ok or sms_ok
    new_status = "sent" if actual_delivery else "approved_demo"

    with db() as conn:
        conn.execute(
            "UPDATE invoices SET status = ?, sent_at = ? WHERE id = ?",
            (new_status, datetime.now().isoformat(timespec="seconds"), invoice_id),
        )

    return {
        "ok": True,
        "status": new_status,
        "invoice_number": invoice.invoice_number,
        "pdf_url": pdf_url,
        "email": email_result,
        "sms": sms_result,
        "message": (
            "Invoice sent."
            if actual_delivery
            else "Invoice approved and saved. Email/SMS were not sent because provider credentials are not configured."
        ),
    }


@router.post("/invoices/{invoice_id}/mark-paid")
def mark_paid(invoice_id: int) -> dict[str, Any]:
    get_invoice(invoice_id)
    with db() as conn:
        conn.execute(
            "UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), invoice_id),
        )
    return {"ok": True, "status": "paid"}


async def send_reminder_for_row(row: sqlite3.Row, reminder_key: str, request: Request | None = None) -> dict[str, Any]:
    invoice = row_to_invoice(row)
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    pdf_url = f"{base}/invoices/{invoice.id}/pdf" if base else ""

    body = (
        f"Hi {invoice.customer.name or 'there'}, reminder: invoice "
        f"{invoice.invoice_number} for ${invoice.total:,.2f} was due {invoice.due_date}."
    )
    if pdf_url:
        body += f" View: {pdf_url}"

    sms_ok, sms_result = await send_sms(invoice.customer.phone, body)

    email_ok = False
    email_result = "Customer email missing"

    if invoice.customer.email:
        brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
        sender = os.getenv(
            "SMTP_FROM",
            os.getenv("BUSINESS_EMAIL", ""),
        ).strip()

        if not brevo_api_key:
            email_result = "BREVO_API_KEY is not configured"
        elif not sender:
            email_result = "SMTP_FROM or BUSINESS_EMAIL is not configured"
        else:
            payload = {
                "sender": {
                    "name": BUSINESS_NAME,
                    "email": sender,
                },
                "to": [
                    {
                        "email": invoice.customer.email,
                        "name": invoice.customer.name or "Customer",
                    }
                ],
                "subject": f"Payment reminder: {invoice.invoice_number}",
                "textContent": body,
            }

            try:
                response = httpx.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={
                        "accept": "application/json",
                        "api-key": brevo_api_key,
                        "content-type": "application/json",
                    },
                    json=payload,
                    timeout=30,
                )

                if response.status_code >= 400:
                    email_result = (
                        f"Brevo reminder failed with status "
                        f"{response.status_code}: {response.text[:250]}"
                    )
                else:
                    email_ok = True
                    email_result = "Reminder email sent through Brevo API"

            except Exception as exc:
                email_result = f"Brevo reminder request failed: {str(exc)[:250]}"

    if sms_ok or email_ok:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO reminder_log (invoice_id, reminder_key, sent_at) VALUES (?, ?, ?)",
                (invoice.id, reminder_key, datetime.now().isoformat(timespec="seconds")),
            )

    return {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "sms": sms_result,
        "email": email_result,
        "sent": sms_ok or email_ok,
    }


@router.post("/run-reminders")
async def run_reminders(request: Request) -> dict[str, Any]:
    expected_secret = os.getenv("REMINDER_RUN_SECRET", "").strip()
    if not expected_secret:
        raise HTTPException(
            status_code=503,
            detail="REMINDER_RUN_SECRET is not configured",
        )

    received_secret = request.headers.get("X-Reminder-Secret", "")
    if received_secret != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid reminder secret")

    today = date.today()
    results = []

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE status IN ('sent', 'overdue')"
        ).fetchall()

    # Mark every unpaid sent invoice overdue as soon as its due date has passed,
    # even when today is not one of the configured reminder milestones.
    overdue_ids = []
    for row in rows:
        due = date.fromisoformat(row["due_date"])
        if due < today and row["status"] == "sent":
            overdue_ids.append(row["id"])

    if overdue_ids:
        with db() as conn:
            for invoice_id in overdue_ids:
                conn.execute(
                    "UPDATE invoices SET status = 'overdue' WHERE id = ?",
                    (invoice_id,),
                )

    for row in rows:
        due = date.fromisoformat(row["due_date"])
        days_overdue = (today - due).days

        reminder_key = None
        if days_overdue == 0:
            reminder_key = "due_today"
        elif days_overdue == 3:
            reminder_key = "overdue_3"
        elif days_overdue == 7:
            reminder_key = "overdue_7"

        if not reminder_key:
            continue

        with db() as conn:
            already = conn.execute(
                "SELECT 1 FROM reminder_log WHERE invoice_id = ? AND reminder_key = ?",
                (row["id"], reminder_key),
            ).fetchone()

        if already:
            continue

        results.append(await send_reminder_for_row(row, reminder_key, request))

    return {
        "checked": len(rows),
        "marked_overdue": len(overdue_ids),
        "reminders": results,
    }


class InvoiceUpdate(BaseModel):
    customer: CustomerData
    items: list[InvoiceItem]
    notes: str = ""
    due_date: str


@router.put("/invoices/{invoice_id}", response_model=InvoiceDraft)
def update_invoice(invoice_id: int, payload: InvoiceUpdate) -> InvoiceDraft:
    get_invoice(invoice_id)

    if not payload.items:
        raise HTTPException(status_code=422, detail="Invoice must contain at least one item.")

    cleaned_items: list[InvoiceItem] = []
    subtotal = 0.0

    for item in payload.items:
        quantity = max(float(item.quantity), 0)
        unit_price = max(float(item.unit_price), 0)
        line_total = round(quantity * unit_price, 2)
        cleaned_items.append(
            InvoiceItem(
                description=item.description.strip() or "Service item",
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
        subtotal += line_total

    subtotal = round(subtotal, 2)
    gst = round(subtotal * DEFAULT_GST_RATE, 2)
    total = round(subtotal + gst, 2)

    delivery = []
    if payload.customer.email:
        delivery.append("email")
    if payload.customer.phone:
        delivery.append("sms")

    sql = (
        "UPDATE invoices SET customer_json = ?, items_json = ?, notes = ?, "
        "due_date = ?, subtotal = ?, gst = ?, total = ?, delivery_json = ?, "
        "pdf_path = NULL WHERE id = ?"
    )

    with db() as conn:
        conn.execute(
            sql,
            (
                payload.customer.model_dump_json(),
                json.dumps([item.model_dump() for item in cleaned_items]),
                payload.notes,
                payload.due_date,
                subtotal,
                gst,
                total,
                json.dumps(delivery),
                invoice_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()

    return row_to_invoice(row)


