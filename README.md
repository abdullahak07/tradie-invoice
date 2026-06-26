<div align="center">

# 🧾 Tradie Invoice

### Turn a message or voice note into a professional quote or tax invoice.

**An AI-assisted invoicing workflow built for Australian tradies — fast enough to use between jobs, structured enough to run a real business.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Production-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Railway](https://img.shields.io/badge/Deployed_on-Railway-0B0D0E?logo=railway&logoColor=white)](https://railway.app/)
[![Status](https://img.shields.io/badge/Status-Active_Development-F59E0B)](#development-status)

[Live deployment](https://tradie-invoice-production-d025.up.railway.app) ·
[API documentation](https://tradie-invoice-production-d025.up.railway.app/docs) ·
[Report an issue](https://github.com/abdullahak07/tradie-invoice/issues)

</div>

---

## What is Tradie Invoice?

Tradie Invoice converts everyday job descriptions into structured, editable quotes and invoices.

Instead of opening accounting software and manually entering every field, a tradie can send a message such as:

> Create an invoice for John Smith. Callout fee $90, replace kitchen tap $180, payment due in 7 days.

Tradie Invoice extracts the customer, line items, prices and payment terms, calculates GST, creates a branded PDF and stores the document for later retrieval.

The product is designed around the way tradies already work: **messages, voice notes and quick corrections from a phone**.

---

## Why it exists

Traditional invoicing tools often require too many screens and too much repetitive data entry, especially when the workday is spent on-site.

Tradie Invoice aims to make the workflow:

- **Faster** — create a draft from one natural-language message.
- **Safer** — review and edit before finalising.
- **Australian-ready** — GST calculations, ABN and payment details.
- **Mobile-first** — operate through Telegram and WhatsApp integrations.
- **Professional** — generate branded quote and invoice PDFs.
- **Traceable** — persist customers, documents, statuses and delivery activity.

---

## Core capabilities

| Capability | What it does |
|---|---|
| 💬 Message-to-invoice | Converts plain-English job descriptions into structured invoice drafts. |
| 📝 Message-to-quote | Creates quotes with expiry dates, line items, GST and totals. |
| ✏️ Conversational editing | Supports follow-up changes to customer details, items, prices, notes and dates. |
| 🎙️ Voice transcription | Converts Telegram or WhatsApp voice notes into text for review. |
| ✅ Voice review flow | Presents **Accept**, **Edit** and **Cancel** actions before creating a document. |
| 🇦🇺 Australian GST | Handles GST-exclusive and GST-inclusive calculations. |
| 📄 Branded PDFs | Generates professional A4 quotes and tax invoices with business branding. |
| 🔁 Quote conversion | Converts an accepted quote into an invoice without re-entering the job. |
| 👷 Trade-aware profiles | Supports trade-specific terminology and permanent business profiles. |
| 🏦 Payment details | Keeps BSB, account number and payment reference separate from service items. |
| 📧 Email delivery | Supports PDF delivery through Brevo when configured. |
| 📱 SMS delivery | Supports invoice links through Twilio when configured. |
| 🗃️ Persistent records | Uses PostgreSQL in production with SQLite support for local development and migration. |
| 🛠️ Admin operations | Includes onboarding, account controls, usage monitoring and infrastructure health views. |

---

## Example workflow

```text
Tradie sends:
"Quote Sarah Lee for 4 LED downlights at $95 each,
callout $110, valid for 14 days."

                         ↓

Tradie Invoice extracts:
Customer: Sarah Lee
Item 1: 4 × LED downlights @ $95
Item 2: 1 × Callout @ $110
Subtotal, GST and total
Quote expiry date

                         ↓

Tradie reviews and edits the draft

                         ↓

A branded PDF is generated and stored

                         ↓

The quote can later be converted into an invoice
```

---

## Product architecture

```mermaid
flowchart LR
    A[Telegram] --> D[FastAPI application]
    B[WhatsApp Cloud API] --> D
    C[Web and Admin UI] --> D

    D --> E[Voice transcription]
    D --> F[Gemini extraction and editing]
    F --> G[Validation and business rules]
    E --> H[Accept / Edit / Cancel]
    H --> F

    G --> I[(PostgreSQL)]
    G --> J[ReportLab PDF engine]

    J --> K[Branded quote or invoice]
    K --> L[Brevo email]
    K --> M[Twilio SMS]
    K --> A
    K --> B
```

### Design principle

AI is used to interpret natural language. Deterministic application logic remains responsible for document totals, GST, persistence, identifiers and PDF generation.

This separation reduces the risk of an AI response directly becoming a financial document without validation.

---

## Supported business profiles

The onboarding system includes trade-specific terminology for:

- Electricians
- Carpenters
- Plumbers
- HVAC and air-conditioning technicians
- Painters
- Landscapers
- Roofers
- Tilers
- Concreters
- Handymen
- Cleaners
- Builders
- Locksmiths
- Mechanics
- Pest-control technicians
- Solar installers
- Other service businesses

A business profile can store its name, owner, ABN, licence number, contact details, default terms, GST preference, payment details, logo and letterhead.

---

## Technology stack

| Layer | Technology |
|---|---|
| API | FastAPI |
| Language | Python 3.11+ |
| Validation | Pydantic |
| AI extraction | Google Gemini |
| Voice processing | Gemini audio/transcription workflow |
| Database | PostgreSQL production, SQLite local/migration support |
| PDF generation | ReportLab |
| Image handling | Pillow |
| Messaging | Telegram Bot API and WhatsApp Cloud API |
| Email | Brevo transactional email API |
| SMS | Twilio |
| Deployment | Railway |
| HTTP client | HTTPX |

---

## Repository structure

```text
tradie-invoice/
├── main.py                     # FastAPI application and router registration
├── telegram_routes.py          # Telegram invoice and quote workflows
├── whatsapp_routes.py          # WhatsApp Cloud API workflows
├── voice_webhooks.py           # Voice-message download and transcription
├── voice_confirm.py            # Pending transcript state and preview controls
├── voice_confirm_routes.py     # Accept, edit and cancel routing
├── invoice_routes.py           # Documents, GST, PDFs and delivery integrations
├── business_onboarding.py      # Business, trade, payment and branding profiles
├── trade_profiles.py           # Trade-specific AI prompt routing
├── trade_letterheads.py        # Profile-aware PDF branding
├── billing.py                  # Plans, credits and usage controls
├── admin_dashboard.py          # Private operational dashboard
├── admin_controls.py           # Administrative account controls
├── db_backend.py               # PostgreSQL/SQLite compatibility layer
├── postgres_schema.py          # Production database schema
├── migrate_sqlite_to_postgres.py
├── requirements.txt
└── tests and verification scripts
```

---

## Local setup

### Prerequisites

- Windows 10/11, macOS or Linux
- Python 3.11 or newer
- A Gemini API key
- A Telegram bot token for Telegram testing
- PostgreSQL for production-style persistence, or SQLite for basic local development

### 1. Clone the repository

```powershell
git clone https://github.com/abdullahak07/tradie-invoice.git
Set-Location .\tradie-invoice
```

### 2. Create and activate a virtual environment

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt
```

### 4. Configure the minimum environment variables

```powershell
$env:GEMINI_API_KEY = "your-gemini-api-key"
$env:TELEGRAM_BOT_TOKEN = "your-telegram-bot-token"
$env:BUSINESS_NAME = "Your Business Name"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8000"
```

For PostgreSQL:

```powershell
$env:DATABASE_URL = "postgresql://user:password@host:5432/database"
```

### 5. Start the application

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

- Application: <http://127.0.0.1:8000>
- API documentation: <http://127.0.0.1:8000/docs>
- Database health: <http://127.0.0.1:8000/health/database>

### 6. Verify startup automatically

```powershell
$Report = [ordered]@{
    CheckedAt = (Get-Date).ToString("o")
    URL       = "http://127.0.0.1:8000/docs"
    Passed    = $false
    Status    = $null
    Error     = $null
}

try {
    $Response = Invoke-WebRequest -Uri $Report.URL -UseBasicParsing -TimeoutSec 15
    $Report.Status = $Response.StatusCode
    $Report.Passed = ($Response.StatusCode -eq 200)
}
catch {
    $Report.Error = $_.Exception.Message
}

$Report | ConvertTo-Json | Set-Content .\startup-check.json

if ($Report.Passed) {
    Write-Host "PASS: Tradie Invoice is running." -ForegroundColor Green
}
else {
    Write-Host "FAIL: Tradie Invoice did not pass the startup check." -ForegroundColor Red
    Get-Content .\startup-check.json
}
```

---

## Configuration

Never commit real API keys, bot tokens, database credentials or customer data.

### Core variables

| Variable | Required | Purpose |
|---|---:|---|
| `GEMINI_API_KEY` | Yes | AI extraction, editing and supported transcription workflows. |
| `TELEGRAM_BOT_TOKEN` | For Telegram | Sends and receives Telegram messages and documents. |
| `DATABASE_URL` | Production | PostgreSQL connection string. |
| `PUBLIC_BASE_URL` | Production | Public base URL used for document links. |
| `BUSINESS_NAME` | Recommended | Business name shown on generated documents. |
| `BUSINESS_EMAIL` | Optional | Business contact and default sender address. |
| `BUSINESS_PHONE` | Optional | Business phone shown on documents. |
| `BUSINESS_ABN` | Optional | Australian Business Number shown on documents. |
| `BUSINESS_ADDRESS` | Optional | Business address shown on documents. |
| `DEFAULT_GST_RATE` | Optional | GST rate as a decimal; defaults to `0.10`. |
| `PILOT_PDF_ONLY` | Optional | Disables external delivery when enabled. |

### Payment variables

| Variable | Purpose |
|---|---|
| `BANK_ACCOUNT_NAME` | Account holder displayed in payment instructions. |
| `BANK_BSB` | Australian BSB. |
| `BANK_ACCOUNT_NUMBER` | Bank account number. |
| `PAYMENT_REFERENCE` | Default payment reference, normally the invoice number. |

### WhatsApp variables

| Variable | Purpose |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | Meta access token. |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Cloud API phone-number identifier. |
| `WHATSAPP_API_VERSION` | Meta Graph API version. |

### Delivery variables

| Variable | Purpose |
|---|---|
| `BREVO_API_KEY` | Sends quote and invoice PDFs by email. |
| `SMTP_FROM` | Verified sender email used by Brevo. |
| `TWILIO_ACCOUNT_SID` | Twilio account identifier. |
| `TWILIO_AUTH_TOKEN` | Twilio authentication token. |
| `TWILIO_FROM_NUMBER` | SMS sender number. |

---

## Railway deployment

A typical Railway start command is:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Recommended production configuration:

1. Connect this GitHub repository to a Railway service.
2. Add a Railway PostgreSQL database.
3. Set `DATABASE_URL` and the required application variables.
4. Set `PUBLIC_BASE_URL` to the Railway service URL.
5. Deploy the `main` branch.
6. Verify `/docs` and `/health/database`.
7. Configure messaging-provider webhooks only after the public deployment is healthy.

Current deployment:

```text
https://tradie-invoice-production-d025.up.railway.app
```

---

## API highlights

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/docs` | Interactive OpenAPI documentation. |
| `GET` | `/health/database` | PostgreSQL connectivity check. |
| `GET` | `/health/migration` | SQLite-to-PostgreSQL migration comparison. |
| `POST` | `/invoice-drafts` | Create an invoice draft from a message. |
| `GET` | `/invoices` | List recent invoices. |
| `GET` | `/invoices/{invoice_id}` | Retrieve one invoice. |
| `GET` | `/invoices/{invoice_id}/pdf` | Generate or retrieve an invoice PDF. |
| `POST` | `/invoices/{invoice_id}/send` | Approve and deliver an invoice when providers are configured. |
| `POST` | `/generate-quote` | Generate a structured quote. |
| `POST` | `/generate-pdf` | Generate a quote PDF. |
| `POST` | `/transcribe` | Transcribe an uploaded audio recording. |

The OpenAPI interface at `/docs` is the authoritative endpoint reference for the deployed version.

---

## Safety and data-quality rules

Financial documents must not silently contain invented information.

The intended extraction rules are:

- Do not invent a customer email address, phone number, name or address.
- Leave genuinely missing fields blank.
- Ask for clarification only when the missing field blocks document creation.
- Treat bank details as payment instructions, never as invoice line items.
- Keep business credentials separate from customer and job data.
- Recalculate subtotal, GST and total deterministically after every edit.
- Require review before sending a document externally.
- Preserve the original document record and status history.

---

## Development status

Tradie Invoice is under active development.

### Working in the current build

- Text-based quote and invoice creation
- Telegram document workflow
- Natural-language editing
- GST and total recalculation
- Quote-to-invoice conversion
- Branded PDF generation
- Business and trade profiles
- PostgreSQL persistence on Railway
- Email and SMS integration paths
- Voice transcription

### Currently being hardened

- Voice transcript **Edit / Accept** state handling
- Prevention of AI-generated customer details that were never provided
- Reliable field-removal commands such as “remove the email”
- Repetition and session-state edge cases during conversational editing
- Final production validation of WhatsApp voice interactions

These items are deliberately shown here rather than presented as completed production behaviour.

---

## Roadmap

- [x] Natural-language invoice drafts
- [x] Natural-language quotes
- [x] Australian GST calculations
- [x] Branded invoice and quote PDFs
- [x] Telegram integration
- [x] PostgreSQL production persistence
- [x] Quote-to-invoice conversion
- [x] Voice transcription
- [ ] Complete voice transcript review hardening
- [ ] Enforce strict no-hallucination field validation
- [ ] Complete end-to-end WhatsApp production validation
- [ ] Expand automated regression coverage
- [ ] Add customer-facing payment and acceptance pages

---

## Testing philosophy

The project includes automated verification and break-test scripts for critical workflows.

Every release should verify at minimum:

1. A valid message produces the expected customer and line items.
2. Missing customer details remain blank rather than being fabricated.
3. GST-inclusive and GST-exclusive totals are correct.
4. Editing an item recalculates all totals.
5. Removing a field actually clears the stored value.
6. PDFs open correctly after a Railway restart.
7. Duplicate webhook deliveries do not create duplicate documents.
8. Voice transcripts are not converted until the user accepts them.
9. A quote converts to one invoice only.
10. External delivery remains disabled when provider credentials are absent.

---

## Security notes

- Keep all secrets in environment variables or a managed secret store.
- Never commit `.env`, credential XML files, API tokens or production exports.
- Protect the admin interface with strong credentials.
- Validate webhook authenticity before expanding public production use.
- Restrict uploaded branding files by type and size.
- Avoid logging full customer messages or payment information.
- Rotate any credential that has appeared in a terminal recording, screenshot or commit.
- Use a dedicated production database with backups.

---

## Contributing

This repository is currently evolving as a focused product build.

When contributing:

1. Create a feature branch.
2. Keep changes limited to one behaviour.
3. Add or update automated tests.
4. Run the relevant verification scripts.
5. Confirm that no real secrets or customer data are included.
6. Open a pull request with the expected and actual behaviour.

---

## Project vision

Tradie Invoice is not intended to be another complicated accounting dashboard.

The goal is a practical assistant that lets a tradie finish the paperwork in the same place the job is described:

> **Say what was done. Check the draft. Send the invoice.**

---

<div align="center">

Built for faster, safer invoicing by Australian trade businesses.

**Repository:** [abdullahak07/tradie-invoice](https://github.com/abdullahak07/tradie-invoice)

</div>
