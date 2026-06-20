$ErrorActionPreference = "Stop"

Write-Host "Permanent Brevo email setup" -ForegroundColor Cyan
Write-Host "Your SMTP key will be encrypted for this Windows user." -ForegroundColor DarkGray
Write-Host ""

$smtpLogin = Read-Host "Brevo SMTP login"
$senderEmail = Read-Host "Verified sender email"
$businessName = Read-Host "Business name"
if ([string]::IsNullOrWhiteSpace($businessName)) {
    $businessName = "Perth Tradie Services"
}

$smtpKey = Read-Host "Brevo SMTP key" -AsSecureString

$credential = New-Object System.Management.Automation.PSCredential($smtpLogin, $smtpKey)
$credential | Export-Clixml -Path ".smtp-credential.xml"

$config = [ordered]@{
    SMTP_HOST     = "smtp-relay.brevo.com"
    SMTP_PORT     = "587"
    SMTP_FROM     = $senderEmail
    BUSINESS_NAME = $businessName
    BUSINESS_EMAIL = $senderEmail
}

$config | ConvertTo-Json | Set-Content -Path ".email-config.json" -Encoding UTF8

@'
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

Write-Host "Email settings loaded." -ForegroundColor Green
Write-Host "Starting Message-to-Invoice..." -ForegroundColor Cyan

py -m uvicorn main:app --reload
'@ | Set-Content -Path "start_app.ps1" -Encoding UTF8

# Keep secrets/config out of Git if a repository is later created.
$gitignoreLines = @(
    ".smtp-credential.xml",
    ".email-config.json"
)

if (Test-Path ".gitignore") {
    $existing = Get-Content ".gitignore"
    foreach ($line in $gitignoreLines) {
        if ($existing -notcontains $line) {
            Add-Content ".gitignore" $line
        }
    }
} else {
    $gitignoreLines | Set-Content ".gitignore" -Encoding UTF8
}

Write-Host ""
Write-Host "Permanent setup completed." -ForegroundColor Green
Write-Host "Created:" -ForegroundColor Yellow
Write-Host "  .smtp-credential.xml  (encrypted SMTP login/key)"
Write-Host "  .email-config.json    (sender and business settings)"
Write-Host "  start_app.ps1         (loads settings and starts FastAPI)"
Write-Host ""
Write-Host "From now on, start the app with:" -ForegroundColor Cyan
Write-Host "powershell.exe -ExecutionPolicy Bypass -File .\start_app.ps1"
