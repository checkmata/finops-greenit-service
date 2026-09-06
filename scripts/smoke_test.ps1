<#
.SYNOPSIS
    Smoke test for finops-greenit-service — runs all layers and verifies endpoints.

.DESCRIPTION
    1. Starts docker compose services (TimescaleDB, Prometheus, fake-exporter, Grafana)
    2. Runs pytest per-file (unit → integration), printing pass/fail per layer
    3. Starts the FastAPI app, curls /health + one endpoint per router
    4. Prints final summary

.NOTES
    What this test DOES prove:
    - Domain engines produce correct output for controlled inputs
    - Adapters correctly map Prometheus JSON → domain objects
    - Adapters correctly persist/retrieve from TimescaleDB
    - Routers correctly call domain engines and return Pydantic responses
    - Health check returns 200 when all dependencies are healthy
    - All Docker services start and become healthy

    What this test does NOT prove:
    - Grafana dashboards visually render (manual step — check localhost:3000)
    - TimescaleDB retention policies work (requires 7+ days to verify)
    - The snapshot scheduler actually persists data in production
    - Prometheus scrape intervals are correct (monitoring gap)
    - OpenShift deployment works (separate test)
    - Load/performance under concurrent requests
    - Network latency between services
#>

param(
    [switch]$SkipDocker,
    [switch]$SkipTests,
    [switch]$SkipApi,
    [int]$ApiPort = 8080
)

$ErrorActionPreference = "Continue"
$rootDir = Split-Path -Parent $PSScriptRoot
$results = @{}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
}

function Write-Pass {
    param([string]$Test)
    Write-Host "  PASS  $Test" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Test, [string]$Reason = "")
    $msg = "  FAIL  $Test"
    if ($Reason) { $msg += " — $Reason" }
    Write-Host $msg -ForegroundColor Red
}

function Write-Info {
    param([string]$Msg)
    Write-Host "  INFO  $Msg" -ForegroundColor Yellow
}

# ===========================================================================
# PART 1 — Start Docker services
# ===========================================================================

if (-not $SkipDocker) {
    Write-Section "PART 1 — Docker Services"

    Write-Info "Building and starting services..."
    Push-Location $rootDir
    docker compose up -d --build 2>&1 | Out-Null
    Pop-Location

    Write-Info "Waiting for services to become healthy..."

    # Wait for TimescaleDB
    $maxWait = 60
    $elapsed = 0
    while ($elapsed -lt $maxWait) {
        $status = docker inspect --format='{{.State.Health.Status}}' finops-timescaledb 2>$null
        if ($status -eq "healthy") { break }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    if ($status -eq "healthy") {
        Write-Pass "TimescaleDB"
        $results["docker-timescaledb"] = "PASS"
    } else {
        Write-Fail "TimescaleDB" "not healthy after ${maxWait}s"
        $results["docker-timescaledb"] = "FAIL"
    }

    # Wait for Prometheus
    $elapsed = 0
    while ($elapsed -lt $maxWait) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:9090/-/healthy" -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { break }
        } catch { }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    if ($elapsed -lt $maxWait) {
        Write-Pass "Prometheus"
        $results["docker-prometheus"] = "PASS"
    } else {
        Write-Fail "Prometheus" "not reachable after ${maxWait}s"
        $results["docker-prometheus"] = "FAIL"
    }

    # Wait for fake-exporter
    $elapsed = 0
    while ($elapsed -lt 30) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:8000/metrics" -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { break }
        } catch { }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    if ($elapsed -lt 30) {
        Write-Pass "Fake Exporter"
        $results["docker-exporter"] = "PASS"
    } else {
        Write-Fail "Fake Exporter" "not reachable after 30s"
        $results["docker-exporter"] = "FAIL"
    }

    # Wait for Grafana
    $elapsed = 0
    while ($elapsed -lt 30) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:3000/api/health" -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { break }
        } catch { }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    if ($elapsed -lt 30) {
        Write-Pass "Grafana"
        $results["docker-grafana"] = "PASS"
    } else {
        Write-Fail "Grafana" "not reachable after 30s"
        $results["docker-grafana"] = "FAIL"
    }
}

# ===========================================================================
# PART 2 — Run pytest per-file (avoids Windows event loop hang)
# ===========================================================================

if (-not $SkipTests) {
    Write-Section "PART 2 — Test Layers"

    Push-Location $rootDir

    # --- Domain layer (unit tests) ---
    Write-Host ""
    Write-Host "  Domain Layer (unit tests):" -ForegroundColor White
    $unitFiles = @(
        "tests/unit/test_finops_engine_unit.py",
        "tests/unit/test_greenit_engine_unit.py",
        "tests/unit/test_recommendation_engine_unit.py",
        "tests/unit/test_snapshot_job.py"
    )
    $unitPass = 0
    $unitFail = 0
    foreach ($file in $unitFiles) {
        $name = [System.IO.Path]::GetFileNameWithoutExtension($file)
        $output = python -m pytest $file -q --tb=line 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            $count = ($output | Select-String "passed" | Select-Object -Last 1).Line -replace '[^0-9]',''
            Write-Pass "$name ($count tests)"
            $unitPass += [int]$count
        } else {
            Write-Fail "$name" "exit code $exitCode"
            $unitFail++
        }
    }
    $results["unit-domain"] = if ($unitFail -eq 0) { "PASS ($unitPass tests)" } else { "FAIL" }

    # --- Adapter layer (integration tests, run individually) ---
    Write-Host ""
    Write-Host "  Adapter Layer (integration tests):" -ForegroundColor White
    $adapterFiles = @(
        "tests/integration/test_prometheus_adapter_integration.py",
        "tests/integration/test_timescale_adapter_integration.py"
    )
    $adapterPass = 0
    $adapterFail = 0
    foreach ($file in $adapterFiles) {
        $name = [System.IO.Path]::GetFileNameWithoutExtension($file)
        $output = python -m pytest $file -q --tb=line 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            $count = ($output | Select-String "passed" | Select-Object -Last 1).Line -replace '[^0-9]',''
            Write-Pass "$name ($count tests)"
            $adapterPass += [int]$count
        } else {
            Write-Fail "$name" "exit code $exitCode"
            $adapterFail++
        }
    }
    $results["integration-adapters"] = if ($adapterFail -eq 0) { "PASS ($adapterPass tests)" } else { "FAIL" }

    # --- API layer (router tests via TestClient) ---
    Write-Host ""
    Write-Host "  API Layer (router tests):" -ForegroundColor White
    $output = python -m pytest tests/integration/test_api.py -q --tb=line 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        $count = ($output | Select-String "passed" | Select-Object -Last 1).Line -replace '[^0-9]',''
        Write-Pass "test_api ($count tests)"
        $results["integration-api"] = "PASS ($count tests)"
    } else {
        Write-Fail "test_api" "exit code $exitCode"
        $results["integration-api"] = "FAIL"
    }

    Pop-Location
}

# ===========================================================================
# PART 3 — Live API smoke test (uvicorn + curl)
# ===========================================================================

if (-not $SkipApi) {
    Write-Section "PART 3 — Live API Smoke Test"

    Push-Location $rootDir

    # Start uvicorn in background
    Write-Info "Starting FastAPI on port $ApiPort..."
    $env:PROMETHEUS_URL = "http://localhost:9090"
    $env:DATABASE_URL = "postgresql://finops:finops_dev_password@localhost:5432/finops_greenit"
    $uvicorn = Start-Process python -ArgumentList "-m uvicorn app.adapters.inbound.api.main:app --port $ApiPort --host 127.0.0.1" `
        -PassThru -NoNewWindow -WorkingDirectory $rootDir

    # Wait for app to start
    $maxWait = 30
    $elapsed = 0
    $appReady = $false
    while ($elapsed -lt $maxWait) {
        Start-Sleep -Seconds 2
        $elapsed += 2
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 3 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                $appReady = $true
                break
            }
        } catch { }
    }

    if ($appReady) {
        # Test /health
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 5
            $body = $resp.Content | ConvertFrom-Json
            if ($body.status -eq "healthy") {
                Write-Pass "GET /health (200, healthy)"
                $results["api-health"] = "PASS"
            } else {
                Write-Fail "GET /health" "status=$($body.status)"
                $results["api-health"] = "FAIL"
            }
        } catch {
            Write-Fail "GET /health" $_.Exception.Message
            $results["api-health"] = "FAIL"
        }

        # Test /api/finops/namespace/team-alpha
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/finops/namespace/team-alpha" -TimeoutSec 5
            $body = $resp.Content | ConvertFrom-Json
            if ($body.namespace -eq "team-alpha" -and $body.pod_count -gt 0) {
                Write-Pass "GET /api/finops/namespace/team-alpha (200, $($body.pod_count) pods)"
                $results["api-finops"] = "PASS"
            } else {
                Write-Fail "GET /api/finops/namespace/team-alpha" "unexpected response"
                $results["api-finops"] = "FAIL"
            }
        } catch {
            $statusCode = $_.Exception.Response.StatusCode.value__
            Write-Fail "GET /api/finops/namespace/team-alpha" "HTTP $statusCode"
            $results["api-finops"] = "FAIL"
        }

        # Test /api/greenit/namespace/team-alpha
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/greenit/namespace/team-alpha" -TimeoutSec 5
            $body = $resp.Content | ConvertFrom-Json
            if ($body.namespace -eq "team-alpha" -and $body.carbon_footprint) {
                Write-Pass "GET /api/greenit/namespace/team-alpha (200)"
                $results["api-greenit"] = "PASS"
            } else {
                Write-Fail "GET /api/greenit/namespace/team-alpha" "unexpected response"
                $results["api-greenit"] = "FAIL"
            }
        } catch {
            $statusCode = $_.Exception.Response.StatusCode.value__
            Write-Fail "GET /api/greenit/namespace/team-alpha" "HTTP $statusCode"
            $results["api-greenit"] = "FAIL"
        }

        # Test /api/recommendations/history
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/recommendations/history" -TimeoutSec 5
            Write-Pass "GET /api/recommendations/history (200)"
            $results["api-recommendations"] = "PASS"
        } catch {
            $statusCode = $_.Exception.Response.StatusCode.value__
            # 404 is acceptable — no recommendations seeded yet
            if ($statusCode -eq 404) {
                Write-Pass "GET /api/recommendations/history (404 — empty, expected)"
                $results["api-recommendations"] = "PASS (empty)"
            } else {
                Write-Fail "GET /api/recommendations/history" "HTTP $statusCode"
                $results["api-recommendations"] = "FAIL"
            }
        }
    } else {
        Write-Fail "FastAPI app" "did not start within ${maxWait}s"
        $results["api-health"] = "FAIL"
        $results["api-finops"] = "FAIL"
        $results["api-greenit"] = "FAIL"
        $results["api-recommendations"] = "FAIL"
    }

    # Stop uvicorn
    Write-Info "Stopping FastAPI..."
    Stop-Process -Id $uvicorn.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    Pop-Location
}

# ===========================================================================
# FINAL SUMMARY
# ===========================================================================

Write-Section "SUMMARY"

$allPassed = $true
foreach ($key in ($results.Keys | Sort-Object)) {
    $val = $results[$key]
    if ($val -like "PASS*") {
        Write-Host "  PASS  $key : $val" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $key : $val" -ForegroundColor Red
        $allPassed = $false
    }
}

Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Cyan
if ($allPassed) {
    Write-Host "  ALL LAYERS PASSED" -ForegroundColor Green
} else {
    Write-Host "  SOME LAYERS FAILED — review above" -ForegroundColor Red
}
Write-Host ("=" * 60) -ForegroundColor Cyan

Write-Host ""
Write-Host "  Manual steps remaining:" -ForegroundColor Yellow
Write-Host "    1. Open Grafana: http://localhost:3000 (admin/admin)" -ForegroundColor Yellow
Write-Host "    2. Verify Prometheus datasource is connected in Grafana" -ForegroundColor Yellow
Write-Host "    3. Create dashboards manually (dashboard JSON is a future task)" -ForegroundColor Yellow
Write-Host "    4. Verify TimescaleDB datasource shows tables in Grafana" -ForegroundColor Yellow
Write-Host ""
