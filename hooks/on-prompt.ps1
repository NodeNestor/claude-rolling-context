# Ensure rolling context proxy is running (Windows)
# Runs on SessionStart — must be fast, non-blocking

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyDir = Join-Path $ScriptDir "..\proxy"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$HookLog = Join-Path $ClaudeDir "rolling-context-hook.log"
$ProxyLog = Join-Path $ClaudeDir "rolling-context-proxy.log"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ProxyUrl = "http://127.0.0.1:$Port"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $HookLog -Value "[$ts] $msg"
}

Log "Hook started. ScriptDir=$ScriptDir ProxyDir=$ProxyDir"
Log "CLAUDE_PLUGIN_ROOT=$($env:CLAUDE_PLUGIN_ROOT)"

# Fast check: is proxy already running?
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) {
            Log "Proxy already running (PID $savedPid)"
            exit 0
        }
        Log "Stale PID file (PID $savedPid not running), removing"
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Ensure ANTHROPIC_BASE_URL is set for future sessions
$currentUrl = [Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")
if (-not $currentUrl) {
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
    Log "Set ANTHROPIC_BASE_URL=$ProxyUrl (user env var)"
} elseif ($currentUrl -notmatch "127\.0\.0\.1.*$Port") {
    [Environment]::SetEnvironmentVariable("ROLLING_CONTEXT_UPSTREAM", $currentUrl, "User")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $ProxyUrl, "User")
    Log "Chaining: upstream=$currentUrl, ANTHROPIC_BASE_URL=$ProxyUrl"
} else {
    Log "ANTHROPIC_BASE_URL already set to $currentUrl"
}

# Check if venv exists
$VenvPython = Join-Path $ProxyDir "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    Log "Venv exists at $VenvPython"
} else {
    Log "Venv NOT found, will create"
}

# Start proxy in background — DO NOT WAIT
$setupScript = @"
`$logFile = '$HookLog'
function Log(`$msg) {
    `$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path `$logFile -Value "[`$ts] [bg] `$msg"
}
try {
    Set-Location '$ProxyDir'
    Log "Background: working dir = `$(Get-Location)"
    if (-not (Test-Path 'venv\Scripts\python.exe')) {
        Log "Creating venv..."
        python -m venv venv 2>&1 | Out-Null
        if (-not (Test-Path 'venv\Scripts\python.exe')) {
            Log "ERROR: venv creation failed!"
            exit 1
        }
        Log "Installing requirements..."
        & .\venv\Scripts\pip.exe install -q -r requirements.txt 2>&1 | Out-Null
        Log "Requirements installed"
    }
    Log "Starting proxy server..."
    `$proc = Start-Process -FilePath '.\venv\Scripts\python.exe' -ArgumentList 'server.py' ``
        -RedirectStandardOutput '$ProxyLog' -RedirectStandardError '$ProxyLog.err' ``
        -WindowStyle Hidden -PassThru
    `$proc.Id | Out-File -FilePath '$PidFile' -NoNewline
    Log "Proxy started with PID `$(`$proc.Id)"
} catch {
    Log "ERROR: `$(`$_.Exception.Message)"
}
"@

Log "Launching background setup..."
Start-Process -FilePath "powershell" -ArgumentList "-ExecutionPolicy", "Bypass", "-Command", $setupScript -WindowStyle Hidden
Log "Background setup launched, hook exiting"

exit 0
