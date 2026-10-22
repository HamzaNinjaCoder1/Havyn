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

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Queue = $env:HAVYN_QUEUE
if (-not $Queue) { $Queue = Join-Path $PSScriptRoot "nightly-queue.json" }
$Results = $env:HAVYN_RESULTS
if (-not $Results) { $Results = Join-Path $RepoRoot "runner_results" }

# A scheduled task starts with the machine/user PATH, which usually does not
# have the repository's virtualenv. Prefer it when present so the task does not
# silently run a system Python that lacks the package.
$Python = $env:HAVYN_PYTHON
if (-not $Python) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { $Python = $venvPython } else { $Python = "python" }
}

function Test-Heartbeat {
    # The Script Engine heartbeats into the engine's mailbox subdir ("bridge" --
    # see network_autopilot.pt.mailbox._MAILBOX_SUBDIR), in whichever of the two
    # runtime dirs its extension resolved at load. A guessed subdir here makes
    # the nightly always time out with PT wide awake.
    $candidates = @(
        (Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "Havyn") "bridge") "alive.txt"),
        (Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "packet-tracer-mcp") "bridge") "alive.txt")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $age = ((Get-Date) - (Get-Item -LiteralPath $candidate).LastWriteTime).TotalSeconds
            if ($age -lt 30) { return $true }
        }
    }
    return $false
}

function Get-FailureDetail([string]$ResultsDir) {
    # A morning notification that says only "exit 2" is not actionable. Name
    # the cases that did not pass, with the runner's own reason.
    $summaryPath = Join-Path $ResultsDir "summary.json"
    if (-not (Test-Path -LiteralPath $summaryPath)) {
        return "no summary.json in $ResultsDir"
    }
    $failedCases = @()
    Get-ChildItem -LiteralPath $ResultsDir -Filter "*.json" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne "summary.json" } |
        ForEach-Object {
            try {
                $result = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
            } catch {
                return
            }
            if ($result.status -ne "pass") {
                $reason = if ($result.detail) { $result.detail } else { $result.status }
                $failedCases += "$($result.case_id): $reason"
            }
        }
    if ($failedCases.Count -gt 0) {
        return ($failedCases -join "; ")
    }
    return "runner reported failure; see $summaryPath"
}

function Test-Channel {
    # A heartbeat file says the extension ticked; it does not prove the file
    # channel can execute a command. A freshly started PT can leave a stale
    # fresh-looking heartbeat while the mailbox is still initializing; the
    # runner then exits 4 before the first case. Probe the real channel.
    $probe = & $Python -c 'from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter; from network_autopilot.pt.mailbox import FileMailbox; print(BridgeAdapter(host_bridge=False, file_bridge=FileMailbox()).ping(timeout=5.0).reachable)' 2>$null
    return ("$probe".Trim() -eq "True")
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
                "${env:ProgramFiles}\Cisco Packet Tracer*\bin\PacketTracer.exe",
                "${env:ProgramFiles(x86)}\Cisco Packet Tracer*\bin\PacketTracer.exe"
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

    # Heartbeat is necessary but not sufficient. Give the mailbox a bounded
    # window to answer a real command before the runner is allowed to start.
    $channelDeadline = (Get-Date).AddMinutes(3)
    while (-not (Test-Channel)) {
        if ((Get-Date) -gt $channelDeadline) {
            throw "Script Engine heartbeat is fresh but the file channel did not answer within 3 minutes"
        }
        Start-Sleep -Seconds 5
    }

    Push-Location $RepoRoot
    try {
        & $Python scripts/havyn_runner.py $Queue --results-dir $Results --adapter bridge
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "havyn_runner exited with code $code; $(Get-FailureDetail $Results)" }

    # The morning needs the report, not just raw per-case JSON. Generate both
    # into the results dir (the uploader copies *.json/*.md), and never let a
    # report failure mask the runner's real verdict. Absolute script paths:
    # a scheduled task's working directory is not the repo root.
    try {
        $scorecard = Join-Path $Results "scorecard.json"
        $scorecardMd = Join-Path $Results "scorecard.md"
        $faultLab = Join-Path $RepoRoot "fault_lab_results\fault_lab.json"
        $scorecardScript = Join-Path $RepoRoot "scripts\scorecard.py"
        $mapScript = Join-Path $RepoRoot "scripts\capability_map.py"
        if (Test-Path -LiteralPath $faultLab) {
            & $Python $scorecardScript $Results --queue $Queue `
                --fault-lab $faultLab --out $scorecard --md $scorecardMd
        } else {
            & $Python $scorecardScript $Results --queue $Queue `
                --out $scorecard --md $scorecardMd
        }
        & $Python $mapScript --results-dir $Results `
            --out-docs (Join-Path $Results "capability-map.md") `
            --out-json (Join-Path $Results "capability-map.json")

        # The runner uploaded raw results before these reports existed.
        # Upload the augmented set again, verified, when a destination is set.
        if ($env:HAVYN_UPLOAD_DIR) {
            $uploadScript = Join-Path $RepoRoot "scripts\upload_results.py"
            & $Python $uploadScript $Results
            if ($LASTEXITCODE -ne 0) {
                throw "report upload failed (destination $env:HAVYN_UPLOAD_DIR)"
            }
        }
    } catch {
        Write-Warning "report generation failed: $($_.Exception.Message)"
        throw
    }
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
