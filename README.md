diff --git a/README.md b/README.md
new file mode 100644
index 0000000000000000000000000000000000000000..4d360c996805e85fd5f3ee2386dc7df9b1647877
--- /dev/null
+++ b/README.md
@@ -0,0 +1,149 @@
+# Perth Tradie Quote AI
+
+Demo-ready SaaS prototype for Perth electricians and HVAC contractors. It turns a job description into an editable quote with Perth pricing defaults, GST, and a branded PDF while keeping the backend local-first with FastAPI, SQLite, Ollama, and optional local Whisper.
+
+## Windows PowerShell setup
+
+From the project folder:
+
+```powershell
+py -3.11 -m venv .venv
+.\.venv\Scripts\Activate.ps1
+py -m pip install --no-cache-dir -r requirements.txt
+```
+
+If your network blocks the default package index, try the explicit PyPI index:
+
+```powershell
+py -m pip install --index-url https://pypi.python.org/simple -r requirements.txt
+```
+
+## Ollama setup
+
+In a separate PowerShell window:
+
+```powershell
+ollama pull llama3.1:8b
+ollama serve
+```
+
+Optional higher-quality model if your GPU setup can load it quickly:
+
+```powershell
+ollama pull llama3.1:70b
+```
+
+The app defaults to `llama3.1:8b` for faster live demos. To run with 70B instead:
+
+```powershell
+$env:OLLAMA_MODEL = "llama3.1:70b"
+py -m uvicorn main:app --reload
+```
+
+Warm-up test:
+
+```powershell
+curl.exe http://localhost:11434/api/generate -d '{"model":"llama3.1:8b","prompt":"Generate a quote for 3 LED downlights in Perth","stream":false}'
+```
+
+## Run FastAPI
+
+In the activated virtual environment:
+
+```powershell
+py -m uvicorn main:app --reload
+```
+
+Open:
+
+- Frontend: <http://127.0.0.1:8000>
+- Swagger docs: <http://127.0.0.1:8000/docs>
+
+## Demo flow
+
+1. Start Ollama.
+2. Start FastAPI.
+3. Open <http://127.0.0.1:8000>.
+4. Enter a job description.
+5. Build Quote.
+6. Edit line items.
+7. Generate PDF.
+
+## Validation prompt
+
+Use this prompt after changes:
+
+```text
+Need 6 LED downlights supplied and installed in living room, replace 2 old power points in kitchen, and install one ceiling fan in bedroom. Customer is in Canning Vale.
+```
+
+Expected result:
+
+- Editable table shows 3 line items.
+- Quantities are 6, 2, and 1.
+- Totals display correctly.
+- Editing labor hours or quantity updates subtotal, GST, and total.
+- Generate PDF returns `application/pdf` and opens/downloads a valid PDF.
+
+## Demo buttons
+
+The frontend includes three demo buttons:
+
+- 2 downlights and a fan
+- Full rewire 3-bedroom house
+- Emergency callout switchboard
+
+## Endpoint curl tests for Windows PowerShell
+
+```powershell
+curl.exe http://127.0.0.1:8000/pricing-defaults
+```
+
+```powershell
+curl.exe http://127.0.0.1:8000/demo-samples
+```
+
+```powershell
+curl.exe -X POST http://127.0.0.1:8000/customers -H "Content-Type: application/json" -d '{"name":"Jane Smith","phone":"0400000000","email":"jane@example.com","address":"Perth WA"}'
+```
+
+```powershell
+curl.exe http://127.0.0.1:8000/customers
+```
+
+```powershell
+curl.exe -X POST http://127.0.0.1:8000/generate-quote -H "Content-Type: application/json" -d '{"text":"Need 6 LED downlights supplied and installed in living room, replace 2 old power points in kitchen, and install one ceiling fan in bedroom. Customer is in Canning Vale.","profile":"Residential","customer":{"name":"Demo Customer"}}'
+```
+
+```powershell
+curl.exe -X POST http://127.0.0.1:8000/generate-pdf -H "Content-Type: application/json" -o quote.pdf -d '{"quote_number":"Q-DEMO","customer":{"name":"Demo Customer"},"line_items":[{"description":"LED downlight supply and install","quantity":2,"location":"Living room","material_unit_cost":22,"labor_hours":1,"hourly_rate":95,"material_markup_percent":20}]}'
+```
+
+For transcription, replace `sample.webm` with a real browser recording:
+
+```powershell
+curl.exe -X POST http://127.0.0.1:8000/transcribe -F audio=@sample.webm
+```
+
+## Live demo checklist
+
+| Check | How | Must pass |
+| --- | --- | --- |
+| Backend starts | `py -m uvicorn main:app --reload` | No errors |
+| Ollama responds | `curl.exe` warm-up test | Returns text after warm-up |
+| Frontend loads | Open `http://127.0.0.1:8000` | SaaS UI renders correctly |
+| Demo buttons work | Tap any demo button | Textarea fills instantly |
+| Quote generates | Build Quote | Editable table appears |
+| Validation prompt works | Use prompt above | 3 rows with quantities 6, 2, 1 |
+| Edits recalculate | Change quantity or labor hours | Totals refresh |
+| PDF opens | Generate PDF | Browser opens/downloads PDF |
+| Docs still work | Open `/docs` | Swagger loads |
+| No Mobirise assets | Browser dev tools Network tab | No `/assets` Mobirise requests |
+
+## Troubleshooting
+
+- **PowerShell activation blocked:** run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then run `.\.venv\Scripts\Activate.ps1` again.
+- **First AI request is slow:** Ollama is loading the model. Run the warm-up curl before a live demo.
+- **Quote misses obvious line items:** use the validation prompt and check `/generate-quote`; the deterministic parser is designed to preserve downlights, power points, and fan rows.
+- **PDF does not open:** check pop-up blocking, then try the `generate-pdf` curl command above.
+- **Browser shows old UI:** open <http://127.0.0.1:8000/?v=new> or hard-refresh the page.
