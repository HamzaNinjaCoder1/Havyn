<#
.SYNOPSIS
    Havyn nightly wrapper (S2.3): PT up -> runner -> PT down -> notify.

Runs from Windows Task Scheduler (see havyn-nightly.xml). Starts Packet
Tracer only when it is not already running, waits bounded for the Script
Engine heartbeat, runs the queue through scripts/havyn_runner.py, then
closes PT again only when this script started it. Never deletes anything:
--clean-workspace always stays an explicit human decision.

Environment (all optional):
  HAVYN_QUEUE        queue file (default: nightly-queue.json next to this script)
  HAVYN_RESULTS      results dir (default: runner_results under the repo)
  HAVYN_NOTIFY_FILE  failure notifications are appended here (default: none)
  HAVYN_NOTIFY_URL   POSTed to on failure (default: none)
#>
param()

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Queue = $env:HAVYN_QUEUE
if (-not $Queue) { $Queue = Join-Path $PSScriptRoot "nightly-queue.json" }
$Results = $env:HAVYN_RESULTS
if (-not $Results) { $Results = Join-Path $RepoRoot "runner_results" }

function Test-Heartbeat {
    # The Script Engine heartbeats into the engine's mailbox subdir ("bridge" --
    # see network_autopilot.pt.mailbox._MAILBOX_SUBDIR), in whichever of the two
    # runtime dirs its extension resolved at load. A guessed subdir here makes
    # the nightly always time out with PT wide awake.
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Havyn" "bridge" "alive.txt"),
        (Join-Path $env:LOCALAPPDATA "packet-tracer-mcp" "bridge" "alive.txt")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $age = ((Get-Date) - (Get-Item -LiteralPath $candidate).LastWriteTime).TotalSeconds
            if ($age -lt 30) { return $true }
        }
    }
    return $false
}

function Notify-Failure([string]$message) {
    $line = "$(Get-Date -Format o) nightly FAILED: $message"
    if ($env:HAVYN_NOTIFY_FILE) {
        Add-Content -LiteralPath $env:HAVYN_NOTIFY_FILE -Value $line
    }
    if ($env:HAVYN_NOTIFY_URL) {
        try {
            Invoke-RestMethod -Method Post -Uri $env:HAVYN_NOTIFY_URL `
                -Body (@{ text = $line } | ConvertTo-Json) `
                -ContentType "application/json" -TimeoutSec 20 | Out-Null
        } catch {
            if ($env:HAVYN_NOTIFY_FILE) {
                Add-Content -LiteralPath $env:HAVYN_NOTIFY_FILE `
                    -Value "$(Get-Date -Format o) notify failed: $($_.Exception.Message)"
            }
        }
    }
    Write-Error $message
}

$weStartedPT = $false
try {
    $pt = Get-Process -Name "PacketTracer" -ErrorAction SilentlyContinue
    if (-not $pt) {
        $ptPath = Get-Command "PacketTracer" -ErrorAction SilentlyContinue
        if (-not $ptPath) {
            $cands = @(
                "${env:ProgramFiles}\Cisco Packet Tracer*\PacketTracer.exe",
                "${env:ProgramFiles(x86)}\Cisco Packet Tracer*\PacketTracer.exe"
            )
            $ptPath = Get-ChildItem $cands -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $ptPath) { throw "Packet Tracer executable not found" }
        Start-Process $ptPath
        $weStartedPT = $true
    }

    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Test-Heartbeat)) {
        if ((Get-Date) -gt $deadline) { throw "no Script Engine heartbeat within 5 minutes" }
        Start-Sleep -Seconds 5
    }

    Push-Location $RepoRoot
    try {
        & python scripts/havyn_runner.py $Queue --results-dir $Results --adapter bridge
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "havyn_runner exited with code $code" }
    Write-Output "nightly complete: queue $Queue"
}
catch {
    Notify-Failure $_.Exception.Message
    exit 1
}
finally {
    if ($weStartedPT) {
        Stop-Process -Name "PacketTracer" -ErrorAction SilentlyContinue
    }
}
