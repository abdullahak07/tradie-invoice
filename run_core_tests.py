from __future__ import annotations

import json
import re
import os
import shutil
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

# Force isolated SQLite mode for tests.
os.environ.pop("DATABASE_URL", None)

import invoice_routes
import telegram_routes
from billing import (
    consume_document_credit,
    get_account_status,
    refund_document_credit,
)


class TestFailure(Exception):
    pass


RESULTS: list[dict] = []


def check(name: str, condition: bool, details: str = "") -> None:
    if not condition:
        raise TestFailure(details or name)
    RESULTS.append({"name": name, "status": "PASS", "details": details})


def run_test(name: str, fn) -> None:
    before = len(RESULTS)
    try:
        fn()
        if len(RESULTS) == before:
            RESULTS.append({"name": name, "status": "PASS", "details": ""})
    except Exception as exc:
        RESULTS.append(
            {
                "name": name,
                "status": "FAIL",
                "details": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )


def setup_isolated_environment() -> Path:
    root = Path(tempfile.mkdtemp(prefix="tradie_invoice_tests_"))
    invoice_routes.DATA_DIR = root / "data"
    invoice_routes.PDF_DIR = invoice_routes.DATA_DIR / "invoices"
    invoice_routes.QUOTE_PDF_DIR = invoice_routes.DATA_DIR / "quotes"
    invoice_routes.DB_PATH = invoice_routes.DATA_DIR / "test.db"

    invoice_routes.DATA_DIR.mkdir(parents=True, exist_ok=True)
    invoice_routes.PDF_DIR.mkdir(parents=True, exist_ok=True)
    invoice_routes.QUOTE_PDF_DIR.mkdir(parents=True, exist_ok=True)

    invoice_routes.init_db()
    telegram_routes.init_telegram_tables()
    return root


def sample_ai_invoice(
    *,
    name: str = "John Smith",
    address: str = "12 King Street Perth",
    email: str = "john@example.com",
    phone: str = "0412345678",
    due_date: str = "",
) -> telegram_routes.AIInvoice:
    return telegram_routes.AIInvoice(
        customer_name=name,
        customer_address=address,
        customer_email=email,
        customer_phone=phone,
        items=[
            telegram_routes.AIItem(
                description="Callout fee",
                quantity=1,
                unit_price=90,
            ),
            telegram_routes.AIItem(
                description="Labour",
                quantity=2,
                unit="hours",
                unit_price=110,
            ),
        ],
        due_date=due_date,
        notes="Test work",
        gst_included=False,
    )


def test_totals() -> None:
    parsed = sample_ai_invoice()
    items = telegram_routes.convert_items(parsed)
    subtotal, gst, total = telegram_routes.calculate_totals(items, False)
    check("Totals subtotal", subtotal == 310.00, str(subtotal))
    check("Totals GST", gst == 31.00, str(gst))
    check("Totals total", total == 341.00, str(total))


def test_invoice_create_and_edit() -> None:
    invoice = telegram_routes.create_ai_invoice(
        "Invoice John Smith callout 90 labour 2x110",
        sample_ai_invoice(),
    )
    check("Invoice random identifier format", bool(re.fullmatch(r"[A-Z]{9}\d{9}", invoice.invoice_number)), invoice.invoice_number)
    check("Invoice initial total", invoice.total == 341.00, str(invoice.total))

    second_invoice = telegram_routes.create_ai_invoice(
        "Second invoice uniqueness test",
        sample_ai_invoice(name="Second Customer", email="second@example.com"),
    )
    check(
        "Invoice identifiers are unique",
        second_invoice.invoice_number != invoice.invoice_number,
        f"{invoice.invoice_number} vs {second_invoice.invoice_number}",
    )

    edited = sample_ai_invoice()
    edited.items[1].quantity = 3
    updated = telegram_routes.update_ai_invoice(
        invoice.id,
        edited,
        "Change labour to 3 hours",
    )
    check("Invoice edit keeps invoice number", updated.invoice_number == invoice.invoice_number)
    check("Invoice edit recalculates", updated.total == 462.00, str(updated.total))


def test_same_name_customer_disambiguation() -> None:
    first = invoice_routes.CustomerData(
        name="John Smith",
        phone="0400000001",
        email="john.one@example.com",
        address="12 King Street Perth",
    )
    second = invoice_routes.CustomerData(
        name="John Smith",
        phone="0400000002",
        email="john.two@example.com",
        address="9 Test Road Canning Vale",
    )
    telegram_routes.save_customer(first)
    telegram_routes.save_customer(second)

    row_one = telegram_routes.find_saved_customer(
        name="John Smith",
        address="12 King Street Perth",
    )
    row_two = telegram_routes.find_saved_customer(
        name="John Smith",
        address="9 Test Road Canning Vale",
    )
    unknown = telegram_routes.find_saved_customer(
        name="John Smith",
        address="55 Different Avenue Joondalup",
    )

    check("Same-name first address", row_one["email"] == "john.one@example.com")
    check("Same-name second address", row_two["email"] == "john.two@example.com")
    check("Different address does not borrow details", unknown is None)


def test_random_quote_identifiers() -> None:
    first = telegram_routes.create_ai_quote(
        "Random quote ID test one",
        sample_ai_invoice(
            name="Quote Customer One",
            email="quote.one@example.com",
        ),
    )
    second = telegram_routes.create_ai_quote(
        "Random quote ID test two",
        sample_ai_invoice(
            name="Quote Customer Two",
            email="quote.two@example.com",
        ),
    )

    check(
        "Quote random identifier format",
        bool(re.fullmatch(r"Q[A-Z]{8}\d{9}", first.quote_number)),
        first.quote_number,
    )
    check(
        "Quote identifiers are unique",
        first.quote_number != second.quote_number,
        f"{first.quote_number} vs {second.quote_number}",
    )


def test_quote_create_edit_convert() -> None:
    quote = telegram_routes.create_ai_quote(
        "Quote John Smith callout 90 labour 2x110",
        sample_ai_invoice(),
    )
    check("Quote number generated", bool(re.fullmatch(r"Q[A-Z]{8}\d{9}", quote.quote_number)), quote.quote_number)
    check("Quote initial status", quote.status == "awaiting_confirmation", quote.status)

    edited = sample_ai_invoice()
    edited.items[1].quantity = 5
    updated = telegram_routes.update_ai_quote(
        quote.id,
        edited,
        "Change labour to 5 hours",
    )
    check("Quote edit stays quote", updated.quote_number == quote.quote_number)
    check("Quote edit total", updated.total == 704.00, str(updated.total))

    invoice = telegram_routes.convert_quote_to_invoice(quote.id)
    check("Quote conversion random identifier", bool(re.fullmatch(r"[A-Z]{9}\d{9}", invoice.invoice_number)), invoice.invoice_number)
    converted_quote = telegram_routes.row_to_quote(
        telegram_routes.get_quote(quote.id)
    )
    check("Quote status converted", converted_quote.status == "converted")
    check("Quote linked to invoice", converted_quote.converted_invoice_id == invoice.id)

    duplicate = telegram_routes.convert_quote_to_invoice(quote.id)
    check("Duplicate conversion reuses invoice", duplicate.id == invoice.id)


def test_quote_expiry() -> None:
    expired_date = (date.today() - timedelta(days=1)).isoformat()
    quote = telegram_routes.create_ai_quote(
        "Quote expired test",
        sample_ai_invoice(
            name="Expired Customer",
            email="expired@example.com",
            phone="",
            address="1 Old Road Perth",
            due_date=expired_date,
        ),
    )
    telegram_routes.refresh_expired_quotes()
    refreshed = telegram_routes.row_to_quote(
        telegram_routes.get_quote(quote.id)
    )
    check("Quote automatically expired", refreshed.status == "expired", refreshed.status)
    check("Quote expired timestamp", bool(refreshed.expired_at))


def test_trial_credit_accounting() -> None:
    channel = "telegram"
    external_id = "billing-test-user"

    before = get_account_status(channel, external_id)
    check(
        "Trial starts with configured credits",
        before["credit_balance"] == before["credit_limit"],
        str(before),
    )

    first = consume_document_credit(
        channel,
        external_id,
        "invoice",
        900001,
    )
    second = consume_document_credit(
        channel,
        external_id,
        "invoice",
        900001,
    )

    check("First PDF consumes one credit", first["charged"] is True)
    check(
        "Duplicate PDF consumes no extra credit",
        second["already_charged"] is True,
    )

    after = get_account_status(channel, external_id)
    check(
        "Credit balance reduced once",
        after["credit_balance"] == before["credit_balance"] - 1,
        str(after),
    )

    refunded = refund_document_credit(
        channel,
        external_id,
        "invoice",
        900001,
    )
    check("Failed PDF credit can be refunded", refunded is True)

    final = get_account_status(channel, external_id)
    check(
        "Refund restores credit",
        final["credit_balance"] == before["credit_balance"],
        str(final),
    )


def test_pdf_generation_lock() -> None:
    invoice = telegram_routes.create_ai_invoice(
        "Invoice lock test",
        sample_ai_invoice(name="Lock Customer"),
    )
    first = telegram_routes.claim_pdf_generation("invoice", invoice.id)
    second = telegram_routes.claim_pdf_generation("invoice", invoice.id)
    check("PDF lock first claim succeeds", first is True)
    check("PDF lock second claim blocked", second is False)

    telegram_routes.release_pdf_generation("invoice", invoice.id)
    third = telegram_routes.claim_pdf_generation("invoice", invoice.id)
    check("PDF lock can retry after release", third is True)


def test_pdf_generation() -> None:
    invoice = telegram_routes.create_ai_invoice(
        "Invoice PDF test",
        sample_ai_invoice(name="PDF Customer"),
    )
    invoice_pdf = invoice_routes.create_pdf(
        invoice_routes.get_invoice(invoice.id)
    )
    check("Invoice PDF generated", invoice_pdf.exists())
    check("Invoice PDF non-empty", invoice_pdf.stat().st_size > 1000, str(invoice_pdf.stat().st_size))

    quote = telegram_routes.create_ai_quote(
        "Quote PDF test",
        sample_ai_invoice(name="Quote PDF Customer"),
    )
    quote_pdf = invoice_routes.create_quote_pdf(quote)
    check("Quote PDF generated", quote_pdf.exists())
    check("Quote PDF non-empty", quote_pdf.stat().st_size > 1000, str(quote_pdf.stat().st_size))


def test_friendly_errors() -> None:
    msg = telegram_routes.friendly_error_message(
        ValueError("No valid priced invoice items were found.")
    )
    check("Friendly missing-price error", "price" in msg.lower())
    check("Raw traceback hidden", "ValueError" not in msg)


def main() -> int:
    root = setup_isolated_environment()
    try:
        tests = [
            ("Totals", test_totals),
            ("Invoice create/edit", test_invoice_create_and_edit),
            ("Same-name customers", test_same_name_customer_disambiguation),
            ("Random quote identifiers", test_random_quote_identifiers),
            ("Quote create/edit/convert", test_quote_create_edit_convert),
            ("Quote expiry", test_quote_expiry),
            ("Trial credit accounting", test_trial_credit_accounting),
            ("PDF generation lock", test_pdf_generation_lock),
            ("PDF generation", test_pdf_generation),
            ("Friendly errors", test_friendly_errors),
        ]
        for name, fn in tests:
            run_test(name, fn)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = sum(1 for item in RESULTS if item["status"] == "PASS")
    failed = sum(1 for item in RESULTS if item["status"] == "FAIL")
    verdict = "PASS" if failed == 0 else "FAIL"

    report = {
        "verdict": verdict,
        "passed": passed,
        "failed": failed,
        "results": RESULTS,
    }

    report_dir = Path("test_reports")
    report_dir.mkdir(exist_ok=True)
    json_path = report_dir / "core_test_report.json"
    text_path = report_dir / "core_test_report.txt"

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 80,
        "TRADIE INVOICE CORE TEST VERDICT",
        f"OVERALL: {verdict}",
        f"PASSED: {passed}",
        f"FAILED: {failed}",
        "=" * 80,
    ]
    for item in RESULTS:
        lines.append(
            f"[{item['status']}] {item['name']}"
            + (f" — {item.get('details', '')}" if item.get("details") else "")
        )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nJSON report: {json_path}")
    print(f"Text report: {text_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
