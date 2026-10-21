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

.PARAMETER Queue
    Queue file the runner consumes. Default runner_s119/queue.json.

.PARAMETER ResultsDir
    Where per-case result JSON is written. Default runner_s119/results.

.PARAMETER NoRestart
    Do not restart the sweep on a failure; just report it. For diagnosis only —
    a -NoRestart run can never emit a PASS verdict.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\s119_gate.ps1
#>
param(
    [int]    $Runs         = 10,
    [double] $LimitSeconds = 300,
    [string] $Spec         = "my-network.spec.json",
    [string] $Queue        = "runner_s119/queue.json",
    [string] $ResultsDir   = "runner_s119/results",
    [switch] $NoRestart
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$gateDir     = Join-Path $RepoRoot "runner_s119"
$resultsPath = Join-Path $RepoRoot $ResultsDir
$queuePath   = Join-Path $RepoRoot $Queue
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
Write-Host "queue       : $Queue"
Write-Host "results dir : $ResultsDir"
Write-Host ""

foreach ($required in @($specPath, $queuePath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        Write-Error "missing required file: $required"
        exit 2
    }
}

# Packet Tracer must be up before we burn the first build: the runner's bridge
# probe returns exit 4 otherwise, and a gate sweep that starts on a dead
# channel produces N useless results.
$heartbeatCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Havyn" "bridge" "alive.txt"),
    (Join-Path $env:LOCALAPPDATA "packet-tracer-mcp" "bridge" "alive.txt")
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

# --------------------------------------------------------------- the sweep

function Invoke-Sweep {
    param([int]$Attempt)

    # A fresh sweep starts from an empty results dir. The runner resumes past
    # any case whose result file already exists, so leaving prior results here
    # would make run02..run10 no-ops reading yesterday's verdicts.
    Get-ChildItem -LiteralPath $resultsPath -Filter "*.json" -ErrorAction SilentlyContinue |
        Remove-Item -Force

    $records   = @()
    $sweepPass = $true

    for ($i = 1; $i -le $Runs; $i++) {
        $id        = "run{0:D2}" -f $i
        $logFile   = Join-Path $logDir ("attempt{0}-{1}.out" -f $Attempt, $id)
        $startedAt = Get-Date

        Write-Host ("--- {0} (attempt {1}) ---" -f $id, $Attempt) -ForegroundColor Yellow

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        & python scripts/havyn_runner.py $Queue `
              --results-dir $ResultsDir `
              --adapter bridge *>&1 | Tee-Object -FilePath $logFile
        $exit = $LASTEXITCODE
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
    records        = $final.records
    note           = ("A run counts only when runner exit is 0, every criterion passes " +
                      "(0 fail, 0 unknown), and the runner's own wall clock is under the " +
                      "limit. Internal phase_timing is deliberately NOT the measured value: " +
                      "it excludes the PT probe and the workspace clean, so it understates " +
                      "real elapsed time.")
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

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
