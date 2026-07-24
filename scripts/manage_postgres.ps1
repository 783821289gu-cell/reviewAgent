param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status",
    [int]$Port = 5432
)

$ErrorActionPreference = "Stop"
$runtimeRoot = $env:REVIEW_AGENT_RUNTIME_ROOT
if (-not $runtimeRoot) {
    $runtimeRoot = [Environment]::GetEnvironmentVariable(
        "REVIEW_AGENT_RUNTIME_ROOT",
        "User"
    )
}
if (-not $runtimeRoot) {
    throw "REVIEW_AGENT_RUNTIME_ROOT is not configured."
}
if ($Port -le 0 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535."
}

$postgresRoot = Join-Path $runtimeRoot "postgresql\17"
$dataDir = Join-Path $runtimeRoot "data\postgres"
$logFile = Join-Path $runtimeRoot "logs\postgresql.log"
$pgCtl = Join-Path $postgresRoot "bin\pg_ctl.exe"

if (-not (Test-Path -LiteralPath $pgCtl)) {
    throw "PostgreSQL executable not found: $pgCtl"
}
if (-not (Test-Path -LiteralPath (Join-Path $dataDir "PG_VERSION"))) {
    throw "PostgreSQL data directory is not initialized: $dataDir"
}

& $pgCtl -D $dataDir status *> $null
$isRunning = $LASTEXITCODE -eq 0

switch ($Action) {
    "start" {
        if ($isRunning) {
            Write-Host "PostgreSQL is already running."
            exit 0
        }
        & $pgCtl -D $dataDir -l $logFile -o "`"-h`" `"127.0.0.1`" `"-p`" `"$Port`"" start
        exit $LASTEXITCODE
    }
    "stop" {
        if (-not $isRunning) {
            Write-Host "PostgreSQL is already stopped."
            exit 0
        }
        & $pgCtl -D $dataDir stop -m fast
        exit $LASTEXITCODE
    }
    "status" {
        if ($isRunning) {
            Write-Host "PostgreSQL is running."
            exit 0
        }
        Write-Host "PostgreSQL is stopped."
        exit 1
    }
}
