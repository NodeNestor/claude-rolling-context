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

function Test-PointsAtUs($url, $port) {
    # True only if url is OUR proxy: loopback AND our port. The old regex
    # "127\.0\.0\.1.*$Port" matched 127.0.0.1:15588 for port 5588, and missed
    # localhost:5588 entirely — which would have chained the proxy to itself.
    # Matching on host alone (as the sh hook did) treats every local model
    # endpoint as the proxy and skips chaining altogether.
    try {
        $full = if ($url -match '://') { $url } else { "http://$url" }
        $u = [System.Uri]$full
        return (@('127.0.0.1', 'localhost', '::1') -contains $u.Host) -and ($u.Port -eq [int]$port)
    } catch {
        return $false
    }
}

Log "Hook started. ProxyDir=$ProxyDir"

# Wire ourselves into Claude Code via HTTPS_PROXY + NODE_EXTRA_CA_CERTS (NOT
# ANTHROPIC_BASE_URL, which trips the Remote Control / GrowthBook gate). All the
# settings.json bookkeeping — CA generation, HTTPS_PROXY single-owner ownership,
# chaining with pii-proxy, plugin defaults, stale-base_url cleanup — lives in
# wire.py so it is one tested implementation shared with the sh hook and pii.
$SettingsFile = Join-Path $ClaudeDir "settings.json"
try {
    $wireOut = & python (Join-Path $ProxyDir "wire.py") --name rolling-context --settings $SettingsFile 2>&1
    foreach ($line in $wireOut) { Log "wire: $line" }
} catch {
    Log "WARNING: wire.py failed to update settings.json: $_"
}

# --- Is the proxy actually SERVING? ------------------------------------------
# Not "does a PID file exist", and not even "is that PID alive". A crashed
# proxy leaves its PID file behind and the OS is free to hand that number to an
# unrelated process, so the liveness check says yes while nothing is listening:
# the hook logs "Proxy already running" and every session that follows fails
# with ConnectionRefused (issue #9). Ask the port instead — probe.py answers
# "ours <version>", "foreign" or "down".
$Probe = Join-Path $ScriptDir "probe.py"

# Timeouts are passed as strings on purpose: a double renders through the
# current culture, so on a comma-decimal locale 0.5 reaches python as "0,5".
function Get-ProxyState([string]$timeout = "2") {
    try { (& python $Probe $Port $timeout | Select-Object -First 1) } catch { "down" }
}
function Get-ProxyPid([string]$timeout = "2") {
    try { (& python $Probe $Port $timeout "pid" | Select-Object -First 1) } catch { "" }
}

function Test-IsOurProxy($processId) {
    # Identity, not just liveness — never kill a process that merely inherited
    # our old PID.
    if (-not $processId) { return $false }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
    return ($p -and $p.CommandLine -like "*server.py*")
}

function Stop-ProxyPid($processId, $source) {
    if (-not $processId) { return }
    if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return }
    if (Test-IsOurProxy $processId) {
        Log "Stopping proxy PID $processId ($source)"
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    } else {
        Log "PID $processId ($source) is alive but is not our proxy — recycled PID, leaving that process alone"
    }
}

function Clear-RecordedProxy([switch]$Serving) {
    # The PID from /health outranks the PID file: it comes from the process
    # actually holding the port, while the file is a copy that a crash, a lost
    # bind race or a manual start can leave pointing anywhere.
    if ($Serving) { Stop-ProxyPid (Get-ProxyPid) "reported by /health" }
    if (Test-Path $PidFile) {
        $savedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($savedPid) { Stop-ProxyPid ([string]$savedPid).Trim() "from the PID file" }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item $VerFile -Force -ErrorAction SilentlyContinue
}

$state = Get-ProxyState 2
if ($state -eq "ours $CurrentVersion") {
    Log "Proxy already running and healthy on :$Port (v$CurrentVersion)"
    exit 0
} elseif ($state -like "ours *") {
    Log "Version changed ($($state -replace '^ours ', '') -> $CurrentVersion), restarting proxy"
    Clear-RecordedProxy -Serving
    # The old proxy owns the port until it actually exits. Starting on top of
    # it would just lose the bind and leave the old version serving while the
    # log claimed a restart.
    # Bounded by the clock, not by a probe count: each probe costs an
    # interpreter start, and the SessionStart hook has 30s in total.
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline -and (Get-ProxyState "0.5") -like "ours *") {
        Start-Sleep -Milliseconds 250
    }
    if ((Get-ProxyState "0.5") -like "ours *") {
        Log "ERROR: the old proxy is still serving on :$Port and could not be stopped — not starting a second one."
        exit 0
    }
} elseif ($state -eq "foreign") {
    # Starting here would only fail to bind, silently, forever. Say so.
    Log "ERROR: port $Port is held by something that is not this proxy — not starting. Free the port or set ROLLING_CONTEXT_PORT to another one."
    exit 0
} else {
    if (Test-Path $PidFile) {
        Log "PID file present but nothing is serving on :$Port — the recorded proxy is gone; restarting"
    }
    Clear-RecordedProxy
}

# Start proxy directly with system python — no venv needed
Log "Starting proxy..."
$proc = Start-Process -FilePath "python" -ArgumentList "server.py" `
    -WorkingDirectory $ProxyDir `
    -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
    -WindowStyle Hidden -PassThru
Log "Proxy started with PID $($proc.Id) (v$CurrentVersion)"

# Confirm it came up, rather than assuming. A start that fails — port taken,
# python missing, a syntax error in the proxy — used to be invisible: the hook
# exited 0, the session was already pointed at this port, and the only symptom
# was ConnectionRefused on every request, with nothing in this log to explain
# it. Bounded at ~8s so a cold start never stalls the session for long.
$deadline = (Get-Date).AddSeconds(8)
while ((Get-Date) -lt $deadline) {
    if ((Get-ProxyState "0.5") -eq "ours $CurrentVersion") {
        # Record the PID /health reports, not the one we spawned: if two
        # sessions started at once, the process that actually won the bind may
        # not be ours, and recording a PID that is already dead is what left
        # the next upgrade unable to stop anything.
        $livePid = Get-ProxyPid "0.5"
        $livePid | Out-File -FilePath $PidFile -NoNewline
        $CurrentVersion | Out-File -FilePath $VerFile -NoNewline
        Log "Proxy healthy on :$Port (v$CurrentVersion, PID $livePid)"
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
Log "ERROR: proxy did not answer on :$Port within ~8s of starting — see $ProxyLog"

exit 0
