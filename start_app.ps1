$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$credentialPath = Join-Path $projectDir ".smtp-credential.xml"
$configPath = Join-Path $projectDir ".email-config.json"

if (-not (Test-Path $credentialPath)) {
    throw "Missing .smtp-credential.xml. Run .\configure_email.ps1 first."
}

if (-not (Test-Path $configPath)) {
    throw "Missing .email-config.json. Run .\configure_email.ps1 first."
}

$credential = Import-Clixml -Path $credentialPath
$config = Get-Content $configPath -Raw | ConvertFrom-Json

$env:SMTP_HOST = [string]$config.SMTP_HOST
$env:SMTP_PORT = [string]$config.SMTP_PORT
$env:SMTP_USER = [string]$credential.UserName
$env:SMTP_PASSWORD = $credential.GetNetworkCredential().Password
$env:SMTP_FROM = [string]$config.SMTP_FROM
$env:BUSINESS_NAME = [string]$config.BUSINESS_NAME
$env:BUSINESS_EMAIL = [string]$config.BUSINESS_EMAIL


$telegramCredentialPath = Join-Path $projectDir ".telegram-credential.xml"
if (Test-Path $telegramCredentialPath) {
    $telegramCredential = Import-Clixml -Path $telegramCredentialPath
    $env:TELEGRAM_BOT_TOKEN = $telegramCredential.GetNetworkCredential().Password
}
Write-Host "Email settings loaded." -ForegroundColor Green

$geminiCredentialPath = Join-Path $projectDir ".gemini-credential.xml"
if (Test-Path $geminiCredentialPath) {
    $geminiCredential = Import-Clixml -Path $geminiCredentialPath
    $env:GEMINI_API_KEY = $geminiCredential.GetNetworkCredential().Password
}
Write-Host "Starting Message-to-Invoice..." -ForegroundColor Cyan

& "$projectDir\.venv\Scripts\python.exe" -m uvicorn main:app --reload



