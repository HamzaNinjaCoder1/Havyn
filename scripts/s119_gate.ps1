<#
.SYNOPSIS
    S1.19 exit gate: N clean reference runs, each recorded with its own
    criteria / wall-clock time / exit code, passing only on a full sweep.

.DESCRIPTION
    The blueprint's S1.19 definition:
        "Workspace saaf karke reference spec 10 baar chalao (runner se) /
         Har run: saare criteria, time, exit code record /
         Ek bhi fail ho to wajah -> fix -> poore 10 dobara"
        Done: "10/10 PASS, har run 5 minute se kam."

    This script runs the gate the way that definition reads, and refuses to
    report success on anything less:

    * It measures the RUNNER'S OWN WALL CLOCK, not the engine's internal
      phase_timing. The two differ: build_havyn.py starts its clock after the
      PT ping probe and after --clean-workspace, so an "internal" 293.9 s is
      ~295 s of real elapsed time. The gate is a wall-clock gate, so that is
      what gets recorded and checked.
    * A run counts only if the runner exited 0 AND the criteria are complete
      (0 fail, 0 unknown) AND the wall time is under the limit.
    * Any single failure restarts the whole count: the loop clears the
      results it wrote and begins again, so "9/10" can never be reported as
      done. Pass -NoRestart to inspect a failing sweep instead.
    * It never edits the spec, never weakens a criterion, never suppresses a
      retry, and never trims the flow-probe schedule to buy time.

    Run it from the repo root on the WINDOWS host, with Packet Tracer open.

.PARAMETER Runs
    How many consecutive passing runs the gate requires. Default 10.

.PARAMETER LimitSeconds
    Per-run wall-clock ceiling. Default 300 (the blueprint's "5 minute se kam").

.PARAMETER Spec
    Reference spec. Default my-network.spec.json.

.PARAMETER ResultsDir
    Where per-case result JSON is written. Default runner_s119/results.

.PARAMETER NoRestart
    Do not restart the sweep on a failure; just report it. For diagnosis only —
    a -NoRestart run can never emit a PASS verdict.

.PARAMETER RestartPT
    Close and relaunch Packet Tracer before the sweep, then wait for a fresh
    Script Engine heartbeat. A long-lived PT session accumulates UI state
    (power-distribution devices, a frozen modal from an earlier failed script)
    that can make a bulk create return true while placing nothing; a fresh
    session is what the nightly gets every night.

.PARAMETER CleanAll
    Opt-in for a controlled gate/Nightly workspace: before every run, delete
    the build-created Power Distribution Devices too, not just the spec
    devices. Keeping them across ten runs measurably slows PT (262 s -> ~300 s
    by run 3). This does not relax the criteria: each run still needs 128/128
    criteria and under 300 s wall clock. Never use it on a workspace with the
    user's own physical-environment devices.

.PARAMETER PtExe
    Packet Tracer executable for -RestartPT. Discovered from the process,
    PATH, or Program Files when omitted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\s119_gate.ps1
#>
param(
    [int]    $Runs         = 10,
    [double] $LimitSeconds = 300,
    [string] $Spec         = "my-network.spec.json",
    [string] $ResultsDir   = "runner_s119/results",
    [string] $Python       = "python",
    [string] $PtExe        = "",
    [switch] $NoRestart,
    [switch] $RestartPT,
    [switch] $CleanAll
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$gateDir     = Join-Path $RepoRoot "runner_s119"
$resultsPath = Join-Path $RepoRoot $ResultsDir
$specPath    = Join-Path $RepoRoot $Spec
$logDir      = Join-Path $gateDir "gate-logs"
$summaryPath = Join-Path $gateDir "s119-gate-summary.json"

New-Item -ItemType Directory -Force -Path $resultsPath | Out-Null
New-Item -ItemType Directory -Force -Path $logDir      | Out-Null

# ---------------------------------------------------------------- preflight

Write-Host "=== S1.19 exit gate ===" -ForegroundColor Cyan
Write-Host "runs        : $Runs"
Write-Host "limit       : $LimitSeconds s wall clock per run"
Write-Host "spec        : $Spec"
Write-Host "results dir : $ResultsDir"
Write-Host ""

foreach ($required in @($specPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        Write-Error "missing required file: $required"
        exit 2
    }
}

# Packet Tracer must be up before we burn the first build: the runner's bridge
# probe returns exit 4 otherwise, and a gate sweep that starts on a dead
# channel produces N useless results.
$heartbeatCandidates = @(
    (Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "Havyn") "bridge") "alive.txt"),
    (Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "packet-tracer-mcp") "bridge") "alive.txt")
)
function Test-Heartbeat {
    foreach ($c in $heartbeatCandidates) {
        if (Test-Path -LiteralPath $c) {
            $age = ((Get-Date) - (Get-Item -LiteralPath $c).LastWriteTime).TotalSeconds
            if ($age -lt 30) { return $true }
        }
    }
    return $false
}

if (-not (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA "packet-tracer-mcp\bridge\alive.txt"))) {
    Write-Host "note: legacy heartbeat path not present; relying on the runner's own probe" -ForegroundColor DarkYellow
}

if ($RestartPT) {
    $running = Get-Process PacketTracer -ErrorAction SilentlyContinue
    if ($running) {
        $running | Stop-Process -Force
        Start-Sleep -Seconds 5
    }
    if (-not $PtExe) {
        $found = Get-Command PacketTracer -ErrorAction SilentlyContinue
        if ($found) { $PtExe = $found.Source }
    }
    if (-not $PtExe) {
        $pattern = Join-Path $env:ProgramFiles "Cisco Packet Tracer*\bin\PacketTracer.exe"
        $PtExe = Get-ChildItem $pattern -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $PtExe) {
        Write-Error "Packet Tracer executable not found; pass -PtExe"
        exit 2
    }
    Write-Host "starting Packet Tracer: $PtExe"
    $startedAt = Get-Date
    Start-Process -FilePath $PtExe
    $deadline = (Get-Date).AddMinutes(5)
    while ($true) {
        $fresh = $false
        foreach ($candidate in $heartbeatCandidates) {
            if (Test-Path -LiteralPath $candidate) {
                $item = Get-Item -LiteralPath $candidate
                if ((Get-Date) - $item.LastWriteTime -lt [TimeSpan]::FromSeconds(30) -and
                    $item.LastWriteTime -ge $startedAt.AddSeconds(-2)) {
                    $fresh = $true
                    break
                }
            }
        }
        if ($fresh) { break }
        if ((Get-Date) -gt $deadline) {
            Write-Error "no fresh Script Engine heartbeat within 5 minutes"
            exit 3
        }
        Start-Sleep -Seconds 5
    }
    # The heartbeat is written by the extension as soon as it loads; the
    # mailbox may need another moment before it can answer a command. Burn a
    # bounded window probing the real channel, not just the file's mtime.
    $reachable = $false
    for ($i = 0; $i -lt 24; $i++) {
        $probe = & $Python -c 'from network_autopilot.adapter.packet_tracer.bridge_adapter import BridgeAdapter; from network_autopilot.pt.mailbox import FileMailbox; print(BridgeAdapter(host_bridge=False, file_bridge=FileMailbox()).ping(timeout=5.0).reachable)' 2>$null
        if ("$probe".Trim() -eq "True") { $reachable = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $reachable) {
        Write-Error "Packet Tracer restarted but the Script Engine channel never answered"
        exit 4
    }
    Write-Host "Packet Tracer restarted; Script Engine channel is reachable." -ForegroundColor Green
}

# --------------------------------------------------------------- the sweep

function Invoke-Sweep {
    param([int]$Attempt)

    # A fresh sweep starts from an empty results dir. The runner resumes past
    # any case whose result file already exists, so leaving prior results here
    # would make run02..run10 no-ops reading yesterday's verdicts. The
    # append-only history is cleared too: same-network consistency must
    # describe THIS sweep, not a mixture with earlier attempts.
    Get-ChildItem -LiteralPath $resultsPath -Filter "*.json" -ErrorAction SilentlyContinue |
        Remove-Item -Force
    $historyPath = Join-Path $resultsPath "history.jsonl"
    if (Test-Path -LiteralPath $historyPath) {
        Remove-Item -LiteralPath $historyPath -Force
    }

    $records   = @()
    $sweepPass = $true

    for ($i = 1; $i -le $Runs; $i++) {
        $id        = "run{0:D2}" -f $i
        $logFile   = Join-Path $logDir ("attempt{0}-{1}.out" -f $Attempt, $id)
        $startedAt = Get-Date

        Write-Host ("--- {0} (attempt {1}) ---" -f $id, $Attempt) -ForegroundColor Yellow

        # One case per invocation. The runner executes every uncompleted case
        # in a queue, so passing the 10-case queue here made invocation 1 run
        # all ten builds and measured the sweep (~43 min) as a single "run".
        # A per-run queue makes the stopwatch measure exactly this case.
        $caseQueue = Join-Path $logDir ("queue-{0}.json" -f $id)
        $caseJson = [ordered]@{
            cases = @([ordered]@{
                id          = $id
                spec        = $Spec
                safety_mode = "autonomous"
                clean_all   = [bool]$CleanAll
            })
        } | ConvertTo-Json -Depth 6
        # -Encoding UTF8 on Windows PowerShell 5.1 writes a BOM. The runner
        # accepts it now, but write the file without one so every consumer
        # (and every human opening it) sees plain JSON.
        [System.IO.File]::WriteAllText(
            $caseQueue, $caseJson, (New-Object System.Text.UTF8Encoding($false))
        )

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        # The runner writes real diagnostics to stderr ("STOPPED: workspace
        # could not be cleaned: ...", "Packet Tracer is not reachable"). Under
        # $ErrorActionPreference = "Stop", PowerShell 5.1 turns native stderr
        # into a terminating NativeCommandError and the gate dies without its
        # summary. The runner's exit code is the verdict; stderr is evidence.
        $previousEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $Python scripts/havyn_runner.py $caseQueue `
              --results-dir $ResultsDir `
              --adapter bridge *>&1 | Tee-Object -FilePath $logFile
        $exit = $LASTEXITCODE
        $ErrorActionPreference = $previousEap
        $sw.Stop()

        $wall = [math]::Round($sw.Elapsed.TotalSeconds, 1)

        # The runner writes the per-case JSON; read the verdict from it rather
        # than parsing console text, so the record is the same artifact the
        # scorecard and capability map consume.
        $resultFile = Join-Path $resultsPath "$id.json"
        $crit = $null; $status = "no-result"
        if (Test-Path -LiteralPath $resultFile) {
            $json   = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
            $status = $json.status
            $crit   = $json.criteria
        }

        $complete = ($null -ne $crit) -and ($crit.fail -eq 0) -and ($crit.unknown -eq 0) `
                    -and ($crit.pass -gt 0) -and ($crit.pass -eq $crit.total)
        $under    = $wall -lt $LimitSeconds
        $ok       = ($exit -eq 0) -and $complete -and $under

        $rec = [ordered]@{
            case_id      = $id
            attempt      = $Attempt
            wall_clock_s = $wall
            limit_s      = $LimitSeconds
            under_limit  = $under
            exit_code    = $exit
            status       = $status
            criteria     = $crit
            complete     = $complete
            passed       = $ok
            started_at   = $startedAt.ToString("o")
            log          = $logFile
        }
        $records += [pscustomobject]$rec

        $colour = if ($ok) { "Green" } else { "Red" }
        Write-Host ("    {0}: wall {1}s  exit {2}  status {3}  -> {4}" -f `
            $id, $wall, $exit, $status, $(if ($ok) { "PASS" } else { "FAIL" })) -ForegroundColor $colour

        if (-not $ok) {
            $sweepPass = $false
            if ($NoRestart) {
                Write-Host "    (-NoRestart: continuing to collect the full picture)" -ForegroundColor DarkYellow
                continue
            }
            # One failure restarts the count after the cause is fixed. We stop
            # here and hand back a failure verdict; the operator fixes the root
            # cause and re-runs, which restarts the sweep. Continuing would
            # produce a misleading mixed set.
            Write-Host "    STOPPING the sweep: a failure restarts the count." -ForegroundColor Red
            break
        }
    }

    return [pscustomobject]@{ sweep_passed = $sweepPass; records = $records }
}

# One sweep per invocation, deliberately. The gate definition is
# "one failure -> fix the cause -> redo all ten", so a failing sweep hands
# back a red verdict and stops: re-running the identical sweep would just
# reproduce the same failure, and auto-retrying would hide a flaky run behind
# a lucky one. The operator fixes the root cause and re-runs this script.
$attempt = 1
$final   = Invoke-Sweep -Attempt $attempt

$passedCount = ($final.records | Where-Object { $_.passed }).Count
$gatePass    = $final.sweep_passed -and ($passedCount -eq $Runs) -and (-not $NoRestart)

$summary = [ordered]@{
    generated_at   = (Get-Date).ToString("o")
    gate           = "S1.19"
    gate_passed    = $gatePass
    runs_required  = $Runs
    runs_passed    = $passedCount
    limit_s        = $LimitSeconds
    spec           = $Spec
    mode           = "live"
    attempts       = $attempt
    no_restart     = [bool]$NoRestart
    clean_all      = [bool]$CleanAll
    records        = $final.records
    note           = ("A run counts only when runner exit is 0, every criterion passes " +
                      "(0 fail, 0 unknown), and the runner's own wall clock is under the " +
                      "limit. Internal phase_timing is deliberately NOT the measured value: " +
                      "it excludes the PT probe and the workspace clean, so it understates " +
                      "real elapsed time.")
}
$summaryJson = $summary | ConvertTo-Json -Depth 8
# No BOM: this is the committed evidence, and every JSON reader in the repo
# (and every `jq`) expects a plain UTF-8 document.
[System.IO.File]::WriteAllText(
    $summaryPath, $summaryJson, (New-Object System.Text.UTF8Encoding($false))
)

Write-Host ""
Write-Host "=== S1.19 gate result ===" -ForegroundColor Cyan
Write-Host ("passed {0}/{1} runs, limit {2}s, attempts {3}" -f $passedCount, $Runs, $LimitSeconds, $attempt)
Write-Host ("summary: {0}" -f $summaryPath)
if ($gatePass) {
    Write-Host "GATE: PASS" -ForegroundColor Green
    exit 0
}
Write-Host "GATE: NOT PASSED" -ForegroundColor Red
exit 1
