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

# Clean ANTHROPIC_BASE_URL from Claude Code settings.json
$SettingsFile = Join-Path $ClaudeDir "settings.json"
if (Test-Path $SettingsFile) {
    try {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json

        if ($settings | Get-Member -Name "env" -MemberType NoteProperty) {
            $existingUrl = $null
            $upstream = $null
            if ($settings.env | Get-Member -Name "ANTHROPIC_BASE_URL" -MemberType NoteProperty) {
                $existingUrl = $settings.env.ANTHROPIC_BASE_URL
            }
            if ($settings.env | Get-Member -Name "ROLLING_CONTEXT_UPSTREAM" -MemberType NoteProperty) {
                $upstream = $settings.env.ROLLING_CONTEXT_UPSTREAM
            }

            if ($existingUrl -and $existingUrl -match "127\.0\.0\.1") {
                if ($upstream) {
                    $settings.env.ANTHROPIC_BASE_URL = $upstream
                    $settings.env.PSObject.Properties.Remove("ROLLING_CONTEXT_UPSTREAM")
                    Write-Host "Restored ANTHROPIC_BASE_URL to $upstream"
                } else {
                    $settings.env.PSObject.Properties.Remove("ANTHROPIC_BASE_URL")
                    Write-Host "Removed ANTHROPIC_BASE_URL"
                }
            } elseif ($upstream) {
                $settings.env.PSObject.Properties.Remove("ROLLING_CONTEXT_UPSTREAM")
            }

            # Remove plugin config vars
            $toRemove = $settings.env.PSObject.Properties | Where-Object { $_.Name -like "ROLLING_CONTEXT_*" } | ForEach-Object { $_.Name }
            foreach ($key in $toRemove) {
                $settings.env.PSObject.Properties.Remove($key)
            }

            $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingsFile -Encoding UTF8
        }
    } catch {
        Write-Host "WARNING: Could not clean settings.json: $_"
    }
}

Write-Host ""
Write-Host "Uninstalled."
