# Install the Rolling Context plugin for Claude Code (Windows)
#
# Run: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProxyDir = Join-Path $ScriptDir "proxy"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"

Write-Host "=== Rolling Context Proxy Installer (Windows) ==="
Write-Host ""

# 1. Check Python is available
Write-Host "[1/3] Checking Python..."
try {
    $pyVersion = python --version 2>&1
    Write-Host "  Found $pyVersion (pure stdlib — no pip install needed)"
} catch {
    Write-Host "  ERROR: Python not found. Install Python 3.7+ and try again."
    exit 1
}

# 2. Configure ANTHROPIC_BASE_URL in Claude Code settings.json
Write-Host "[2/3] Configuring Claude Code settings.json..."
$ProxyUrl = "http://127.0.0.1:$Port"
$SettingsFile = Join-Path $ClaudeDir "settings.json"
if (-not (Test-Path $ClaudeDir)) {
    New-Item -ItemType Directory -Path $ClaudeDir -Force | Out-Null
}

try {
    if (Test-Path $SettingsFile) {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }

    if (-not ($settings | Get-Member -Name "env" -MemberType NoteProperty)) {
        $settings | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{})
    }

    $existingUrl = $null
    if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
        $existingUrl = $settings.env.ANTHROPIC_BASE_URL
    }

    if (-not $existingUrl) {
        $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
        Write-Host "  Set ANTHROPIC_BASE_URL=$ProxyUrl"
    } elseif ($existingUrl -notmatch "127\.0\.0\.1.*$Port") {
        $settings.env | Add-Member -NotePropertyName "ROLLING_CONTEXT_UPSTREAM" -NotePropertyValue $existingUrl -Force
        $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue $ProxyUrl -Force
        Write-Host "  Chaining: ANTHROPIC_BASE_URL=$ProxyUrl -> upstream=$existingUrl"
    } else {
        Write-Host "  ANTHROPIC_BASE_URL already set"
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
    Write-Host "  Settings written to $SettingsFile"
} catch {
    Write-Host "  ERROR: Could not update settings.json: $_"
    exit 1
}

# 3. Register plugin
Write-Host "[3/3] Registering Claude Code plugin..."
$PluginsDir = Join-Path $ClaudeDir "plugins"
$PluginLink = Join-Path $PluginsDir "rolling-context"
if (-not (Test-Path $PluginsDir)) {
    New-Item -ItemType Directory -Path $PluginsDir -Force | Out-Null
}
if (Test-Path $PluginLink) {
    Remove-Item $PluginLink -Recurse -Force
}
cmd /c mklink /J "$PluginLink" "$ScriptDir" | Out-Null
Write-Host "  Plugin linked at $PluginLink"

Write-Host ""
Write-Host "=== Installation Complete ==="
Write-Host ""
Write-Host "The proxy will auto-start when you launch Claude Code."
Write-Host "To start it manually: cd $ProxyDir && python server.py"
Write-Host ""
Write-Host "Configuration (via environment variables):"
Write-Host "  ROLLING_CONTEXT_PORT    = $Port"
$trigger = if ($env:ROLLING_CONTEXT_TRIGGER) { $env:ROLLING_CONTEXT_TRIGGER } else { "80000" }
$target = if ($env:ROLLING_CONTEXT_TARGET) { $env:ROLLING_CONTEXT_TARGET } else { "40000" }
Write-Host "  ROLLING_CONTEXT_TRIGGER = $trigger tokens"
Write-Host "  ROLLING_CONTEXT_TARGET  = $target tokens"
Write-Host ""
Write-Host "Start a new Claude Code session to activate the proxy."
