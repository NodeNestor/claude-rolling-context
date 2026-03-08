# Ensure rolling context proxy is running (Windows)
# Pure stdlib — no venv needed, just python

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

Log "Hook started. ProxyDir=$ProxyDir"

# Fast check: is proxy already running?
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) {
            Log "Proxy already running (PID $savedPid)"
            exit 0
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Update Claude Code settings.json with ANTHROPIC_BASE_URL
$SettingsFile = Join-Path $ClaudeDir "settings.json"
try {
    if (Test-Path $SettingsFile) {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }

    # Ensure env object exists
    if (-not ($settings | Get-Member -Name "env" -MemberType NoteProperty)) {
        $settings | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{})
    }

    $existingUrl = $null
    if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
        $existingUrl = $settings.env.ANTHROPIC_BASE_URL
    }

    if (-not $existingUrl) {
        $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
        Log "Set ANTHROPIC_BASE_URL=$ProxyUrl (settings.json)"
    } elseif ($existingUrl -notmatch "127\.0\.0\.1.*$Port") {
        # Save existing URL as upstream
        $settings.env | Add-Member -NotePropertyName "ROLLING_CONTEXT_UPSTREAM" -NotePropertyValue $existingUrl -Force
        $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
        Log "Chaining: upstream=$existingUrl (settings.json)"
    } else {
        Log "ANTHROPIC_BASE_URL already set (settings.json)"
    }

    # Set plugin config defaults (only if not already present)
    $defaults = @{
        "ROLLING_CONTEXT_PORT"    = "5588"
        "ROLLING_CONTEXT_TRIGGER" = "100000"
        "ROLLING_CONTEXT_TARGET"  = "40000"
        "ROLLING_CONTEXT_MODEL"   = "claude-haiku-4-5-20251001"
    }
    foreach ($key in $defaults.Keys) {
        if (-not ($settings.env | Get-Member -Name $key -MemberType NoteProperty)) {
            $settings.env | Add-Member -NotePropertyName $key -NotePropertyValue $defaults[$key]
        }
    }

    $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingsFile -Encoding UTF8
} catch {
    Log "WARNING: Could not update settings.json: $_"
}

# Start proxy directly with system python — no venv needed
Log "Starting proxy..."
$proc = Start-Process -FilePath "python" -ArgumentList "server.py" `
    -WorkingDirectory $ProxyDir `
    -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
    -WindowStyle Hidden -PassThru
$proc.Id | Out-File -FilePath $PidFile -NoNewline
Log "Proxy started with PID $($proc.Id)"

exit 0
