# Uninstall the Rolling Context plugin (Windows)
#
# Run: powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"

$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$PidFile = Join-Path $ClaudeDir "rolling-context-proxy.pid"
$LogFile = Join-Path $ClaudeDir "rolling-context-proxy.log"
$PluginLink = Join-Path $ClaudeDir "plugins\rolling-context"
$MarketplaceCache = Join-Path $ClaudeDir "plugins\cache\rolling-context-marketplace"
$MarketplaceDir = Join-Path $ClaudeDir "plugins\marketplaces\rolling-context-marketplace"

Write-Host "=== Uninstalling Rolling Context ==="

# Stop proxy if running
if (Test-Path $PidFile) {
    $pid = Get-Content $PidFile
    try {
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $pid -Force
            Write-Host "Stopped proxy (PID $pid)"
        }
    } catch {}
    Remove-Item $PidFile -Force
}

# Remove log files
Remove-Item $LogFile -Force -ErrorAction SilentlyContinue
Remove-Item "$LogFile.err" -Force -ErrorAction SilentlyContinue

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
        $json | ConvertTo-Json -Depth 10 | Set-Content $InstalledFile
        Write-Host "Removed from installed plugins"
    }
}

# Clean known_marketplaces.json
$MarketplacesFile = Join-Path $ClaudeDir "plugins\known_marketplaces.json"
if (Test-Path $MarketplacesFile) {
    $json = Get-Content $MarketplacesFile -Raw | ConvertFrom-Json
    if ($json.PSObject.Properties["rolling-context-marketplace"]) {
        $json.PSObject.Properties.Remove("rolling-context-marketplace")
        $json | ConvertTo-Json -Depth 10 | Set-Content $MarketplacesFile
        Write-Host "Removed marketplace"
    }
}

# Restore ANTHROPIC_BASE_URL — if we chained, restore the upstream; otherwise remove
$upstream = [Environment]::GetEnvironmentVariable("ROLLING_CONTEXT_UPSTREAM", "User")
$current = [Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")
if ($current -and $current -match "127\.0\.0\.1.*5588") {
    if ($upstream) {
        [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $upstream, "User")
        [Environment]::SetEnvironmentVariable("ROLLING_CONTEXT_UPSTREAM", $null, "User")
        Write-Host "Restored ANTHROPIC_BASE_URL to $upstream"
    } else {
        [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")
        Write-Host "Removed ANTHROPIC_BASE_URL"
    }
}

Write-Host ""
Write-Host "Uninstalled. Restart your terminal to complete."
