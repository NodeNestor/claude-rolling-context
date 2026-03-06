# Ensure rolling context proxy is running before each prompt (Windows)
# Fast path: if proxy is already up, exits in <100ms

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyDir = Join-Path $ScriptDir "..\proxy"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$LogFile = Join-Path $ClaudeDir "rolling-context-proxy.log"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ProxyUrl = "http://127.0.0.1:$Port"

# Fast check: is proxy already running?
try {
    $response = Invoke-WebRequest -Uri "$ProxyUrl/health" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
    if ($response.StatusCode -eq 200) { exit 0 }
} catch {}

# Also check PID file
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) { exit 0 }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Ensure ANTHROPIC_BASE_URL is set for future sessions
$currentUrl = [Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")
if (-not $currentUrl) {
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
} elseif ($currentUrl -notmatch "127\.0\.0\.1.*$Port") {
    [Environment]::SetEnvironmentVariable("ROLLING_CONTEXT_UPSTREAM", $currentUrl, "User")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
}

# Set up venv if needed, then start proxy in background
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

# Wait for proxy to be ready (up to 15s for first-time venv setup)
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -Uri "$ProxyUrl/health" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        if ($response.StatusCode -eq 200) { exit 0 }
    } catch {}
}

exit 0
