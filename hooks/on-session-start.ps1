# Auto-start the rolling context proxy if it's not already running (Windows)
# Handles chaining: if ANTHROPIC_BASE_URL already points elsewhere, sets that as upstream.

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyDir = Join-Path $ScriptDir "..\proxy"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$LogFile = Join-Path $ClaudeDir "rolling-context-proxy.log"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ProxyUrl = "http://127.0.0.1:$Port"

# Handle ANTHROPIC_BASE_URL chaining
$currentUrl = [Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")
if (-not $currentUrl) {
    # Not set at all — just set it to our proxy
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
} elseif ($currentUrl -notmatch "127\.0\.0\.1.*$Port") {
    # Set to something else (another proxy/router) — chain through it
    [Environment]::SetEnvironmentVariable("ROLLING_CONTEXT_UPSTREAM", $currentUrl, "User")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
}

# Check if proxy is already running via PID
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) { exit 0 }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Check if something is already listening on the port
try {
    $response = Invoke-WebRequest -Uri "$ProxyUrl/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    if ($response.StatusCode -eq 200) { exit 0 }
} catch {}

# Set up venv if needed, then start proxy — all in background to avoid timeout
$setupScript = @"
Set-Location '$ProxyDir'
if (-not (Test-Path 'venv\Scripts\python.exe')) {
    python -m venv venv 2>&1 | Out-Null
    .\venv\Scripts\pip.exe install -q -r requirements.txt 2>&1 | Out-Null
}
`$proc = Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'server.py' ``
    -RedirectStandardOutput '$LogFile' -RedirectStandardError '$LogFile.err' ``
    -WindowStyle Hidden -PassThru
`$proc.Id | Out-File -FilePath '$PidFile' -NoNewline
"@

Start-Process -FilePath "powershell" -ArgumentList "-ExecutionPolicy", "Bypass", "-Command", $setupScript -WindowStyle Hidden

exit 0
