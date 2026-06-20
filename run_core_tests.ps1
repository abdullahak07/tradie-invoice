$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
$report = ".\test_reports\core_test_report.json"

if (-not (Test-Path $python)) {
    Write-Host "[FAIL] Virtual environment Python not found: $python" -ForegroundColor Red
    exit 1
}

& $python -m py_compile `
    .\invoice_routes.py `
    .\telegram_routes.py `
    .\postgres_schema.py `
    .\run_core_tests.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Python syntax verification failed." -ForegroundColor Red
    exit 1
}

Write-Host "[PASS] Python syntax verification passed." -ForegroundColor Green

& $python .\run_core_tests.py
$testExit = $LASTEXITCODE

if (-not (Test-Path $report)) {
    Write-Host "[FAIL] Machine-readable test report was not generated." -ForegroundColor Red
    exit 1
}

$result = Get-Content $report -Raw | ConvertFrom-Json

if ($testExit -eq 0 -and $result.verdict -eq "PASS") {
    Write-Host "`n[PASS] OVERALL VERDICT: ALL CORE TESTS PASSED" -ForegroundColor Green
    Write-Host "Report: $report"
    exit 0
}

Write-Host "`n[FAIL] OVERALL VERDICT: CORE TEST FAILURE" -ForegroundColor Red
Write-Host "Passed: $($result.passed)"
Write-Host "Failed: $($result.failed)"
Write-Host "Report: $report"
exit 1
