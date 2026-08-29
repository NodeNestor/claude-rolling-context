# Uninstall the Rolling Context plugin (Windows)
#
# Run: powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "SilentlyContinue"

$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$PluginLink = Join-Path $ClaudeDir "plugins\rolling-context"
$MarketplaceCache = Join-Path $ClaudeDir "plugins\cache\rolling-context-marketplace"
$MarketplaceDir = Join-Path $ClaudeDir "plugins\marketplaces\rolling-context-marketplace"
$Port = if ($env:ROLLING_CONTEXT_PORT) { $env:ROLLING_CONTEXT_PORT } else { "5588" }

Write-Host "=== Uninstalling Rolling Context ==="

# Stop proxy — try PID file first, then find by port
$stopped = $false
if (Test-Path $PidFile) {
    $proxyPid = Get-Content $PidFile
    $proc = Get-Process -Id $proxyPid -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $proxyPid -Force
        Write-Host "Stopped proxy (PID $proxyPid)"
        $stopped = $true
    }
    Remove-Item $PidFile -Force
}
Remove-Item (Join-Path $ClaudeDir "rolling-context-proxy.version") -Force -ErrorAction SilentlyContinue
if (-not $stopped) {
    $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($conns) {
        $conns | ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Stopped proxy on port $Port"
    }
}

# Remove all log files
Remove-Item (Join-Path $ClaudeDir "rolling-context-proxy.log") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ClaudeDir "rolling-context-proxy.log.err") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ClaudeDir "rolling-context-debug.log") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ClaudeDir "rolling-context-hook.log") -Force -ErrorAction SilentlyContinue

# Remove plugin link (manual install)
if (Test-Path $PluginLink) {
    Remove-Item $PluginLink -Recurse -Force
    Write-Host "Removed plugin link"
}

# Remove marketplace-installed plugin cache
if (Test-Path $MarketplaceCache) {
    Remove-Item $MarketplaceCache -Recurse -Force
    Write-Host "Removed marketplace plugin cache"
}

# Remove marketplace registration
if (Test-Path $MarketplaceDir) {
    Remove-Item $MarketplaceDir -Recurse -Force
    Write-Host "Removed marketplace registration"
}

# Clean installed_plugins.json
$InstalledFile = Join-Path $ClaudeDir "plugins\installed_plugins.json"
if (Test-Path $InstalledFile) {
    $json = Get-Content $InstalledFile -Raw | ConvertFrom-Json
    if ($json.plugins.PSObject.Properties["rolling-context@rolling-context-marketplace"]) {
        $json.plugins.PSObject.Properties.Remove("rolling-context@rolling-context-marketplace")
        # Bare Set-Content defaults to ANSI on Windows PowerShell 5.1, which
        # mangles any non-ASCII path in Claude Code's own plugin registry, and
        # -Encoding UTF8 would add a BOM. Write BOM-less UTF-8 explicitly.
        [System.IO.File]::WriteAllText($InstalledFile, ($json | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Removed from installed plugins"
    }
}

# Clean known_marketplaces.json
$MarketplacesFile = Join-Path $ClaudeDir "plugins\known_marketplaces.json"
if (Test-Path $MarketplacesFile) {
    $json = Get-Content $MarketplacesFile -Raw | ConvertFrom-Json
    if ($json.PSObject.Properties["rolling-context-marketplace"]) {
        $json.PSObject.Properties.Remove("rolling-context-marketplace")
        [System.IO.File]::WriteAllText($MarketplacesFile, ($json | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Removed marketplace"
    }
}

# Unwire from Claude Code settings.json — removes HTTPS_PROXY / chaining /
# NODE_EXTRA_CA_CERTS (repairing the chain so pii-proxy keeps working if it is
# still installed). One tested implementation in wire.py, shared with the sh
# uninstaller.
$SettingsFile = Join-Path $ClaudeDir "settings.json"
$ProxyDir = Join-Path $PSScriptRoot "proxy"
if (Test-Path $SettingsFile) {
    try {
        $out = & python (Join-Path $ProxyDir "wire.py") --name rolling-context --unwire --settings $SettingsFile 2>&1
        foreach ($line in $out) { Write-Host "  $line" }
        # Remove leftover plugin config knobs (routing keys are handled by wire).
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json
        if ($settings | Get-Member -Name "env" -MemberType NoteProperty) {
            $toRemove = $settings.env.PSObject.Properties | Where-Object { $_.Name -like "ROLLING_CONTEXT_*" } | ForEach-Object { $_.Name }
            foreach ($key in $toRemove) { $settings.env.PSObject.Properties.Remove($key) }
            [System.IO.File]::WriteAllText($SettingsFile, ($settings | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
        }
    } catch {
        Write-Host "WARNING: Could not clean settings.json: $_"
    }
}

Write-Host ""
Write-Host "Uninstalled."
