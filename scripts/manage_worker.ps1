param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
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

$python = Join-Path $runtimeRoot "venv\Scripts\python.exe"
$pidFile = Join-Path $runtimeRoot "logs\review-worker.pid"
$stdoutLog = Join-Path $runtimeRoot "logs\review-worker.stdout.log"
$stderrLog = Join-Path $runtimeRoot "logs\review-worker.stderr.log"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable not found: $python"
}

function Get-ManagedWorker {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    $workerPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    return Get-Process -Id $workerPid -ErrorAction SilentlyContinue
}

function Remove-WorkerHeartbeat {
    $redisUrl = $env:REVIEW_AGENT_REDIS_URL
    if (-not $redisUrl) {
        $redisUrl = [Environment]::GetEnvironmentVariable(
            "REVIEW_AGENT_REDIS_URL",
            "User"
        )
    }
    if (-not $redisUrl) {
        return
    }
    $queueName = $env:REVIEW_AGENT_RQ_QUEUE
    if (-not $queueName) {
        $queueName = "review-agent"
    }
    $redisCli = Join-Path $runtimeRoot (
        "redis\8.8.0\Redis-8.8.0-Windows-x64-msys2\redis-cli.exe"
    )
    if (Test-Path -LiteralPath $redisCli) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $redisCli -u $redisUrl DEL "review-agent:worker-heartbeat:$queueName" 2> $null | Out-Null
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
}

$workerProcess = Get-ManagedWorker
switch ($Action) {
    "start" {
        if ($null -ne $workerProcess) {
            Write-Host "RQ worker is already running."
            exit 0
        }
        foreach ($name in @(
            "REVIEW_AGENT_DATABASE_URL",
            "REVIEW_AGENT_REDIS_URL",
            "REVIEW_AGENT_LLM_API_KEY"
        )) {
            if (-not (Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue)) {
                $userValue = [Environment]::GetEnvironmentVariable($name, "User")
                if ($userValue) {
                    Set-Item -LiteralPath "Env:$name" -Value $userValue
                }
            }
        }
        $env:PYTHONPATH = Join-Path $projectRoot "backend\app"
        if (-not $env:REVIEW_AGENT_UPLOAD_DIR) {
            $env:REVIEW_AGENT_UPLOAD_DIR = Join-Path $runtimeRoot "data\uploads"
        }
        if (-not $env:REVIEW_AGENT_RUNTIME_LOG_FILE) {
            $env:REVIEW_AGENT_RUNTIME_LOG_FILE = Join-Path $runtimeRoot "logs\review_agent.log"
        }
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList "-m workers.review_worker" `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
        [System.IO.File]::WriteAllText(
            $pidFile,
            [string]$process.Id,
            [System.Text.UTF8Encoding]::new($false)
        )
        Start-Sleep -Seconds 1
        if ($process.HasExited) {
            throw "RQ worker exited during startup; inspect $stderrLog."
        }
        Write-Host "RQ worker started with PID $($process.Id)."
        exit 0
    }
    "stop" {
        if ($null -eq $workerProcess) {
            Remove-WorkerHeartbeat
            Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
            Write-Host "RQ worker is already stopped."
            exit 0
        }
        Stop-Process -Id $workerProcess.Id
        $workerProcess.WaitForExit()
        Remove-WorkerHeartbeat
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        Write-Host "RQ worker stopped."
        exit 0
    }
    "status" {
        if ($null -ne $workerProcess) {
            Write-Host "RQ worker is running with PID $($workerProcess.Id)."
            exit 0
        }
        Write-Host "RQ worker is stopped."
        exit 1
    }
}
