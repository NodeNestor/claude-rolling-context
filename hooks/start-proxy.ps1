# Ensure rolling context proxy is running (Windows)
# Pure stdlib — no venv needed, just python

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyDir = Join-Path $ScriptDir "..\proxy"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$VerFile = Join-Path $ClaudeDir "rolling-context-proxy.version"
$HookLog = Join-Path $ClaudeDir "rolling-context-hook.log"
$ProxyLog = Join-Path $ClaudeDir "rolling-context-proxy.log"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ProxyUrl = "http://127.0.0.1:$Port"
$PluginJson = Join-Path $ScriptDir "..\.claude-plugin\plugin.json"
$CurrentVersion = if (Test-Path $PluginJson) { (Get-Content $PluginJson -Raw | ConvertFrom-Json).version } else { "unknown" }

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $HookLog -Value "[$ts] $msg"
}

Log "Hook started. ProxyDir=$ProxyDir"

# Always update settings.json first (even if proxy is already running)
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

# Check if proxy is already running
if (Test-Path $PidFile) {
    $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($savedPid) {
        $proc = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($proc) {
            # Check if version changed — restart if so
            $runningVersion = if (Test-Path $VerFile) { Get-Content $VerFile -ErrorAction SilentlyContinue } else { "" }
            if ($runningVersion -eq $CurrentVersion) {
                Log "Proxy already running (PID $savedPid, v$runningVersion)"
                exit 0
            }
            Log "Version changed ($runningVersion -> $CurrentVersion), restarting proxy (PID $savedPid)"
            Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $VerFile -Force -ErrorAction SilentlyContinue
}

# Start proxy directly with system python — no venv needed
Log "Starting proxy..."
$proc = Start-Process -FilePath "python" -ArgumentList "server.py" `
    -WorkingDirectory $ProxyDir `
    -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
    -WindowStyle Hidden -PassThru
$proc.Id | Out-File -FilePath $PidFile -NoNewline
$CurrentVersion | Out-File -FilePath $VerFile -NoNewline
Log "Proxy started with PID $($proc.Id) (v$CurrentVersion)"

exit 0
