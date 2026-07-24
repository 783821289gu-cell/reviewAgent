param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status",
    [int]$Port = 6379
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

$redisRoot = Join-Path $runtimeRoot "redis\8.8.0\Redis-8.8.0-Windows-x64-msys2"
$dataDir = Join-Path $runtimeRoot "data\redis"
$logFile = Join-Path $runtimeRoot "logs\redis.log"
$configFile = Join-Path $runtimeRoot "redis\redis.conf"
$redisServer = Join-Path $redisRoot "redis-server.exe"
$redisCli = Join-Path $redisRoot "redis-cli.exe"

function ConvertTo-MsysPath([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $drive = $resolved.Substring(0, 1).ToLowerInvariant()
    $remainder = $resolved.Substring(2).Replace("\", "/")
    return "/cygdrive/$drive$remainder"
}

function Test-RedisReady {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $redisCli
    $startInfo.Arguments = "-h 127.0.0.1 -p $Port -t 0.2 ping"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    $ready = $process.ExitCode -eq 0 -and $process.StandardOutput.ReadToEnd().Trim() -eq "PONG"
    $process.Dispose()
    return $ready
}

if (-not (Test-Path -LiteralPath $redisServer)) {
    throw "Redis executable not found: $redisServer"
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFile) | Out-Null

$configLines = @(
    "bind 127.0.0.1"
    "protected-mode yes"
    "port $Port"
    "dir `"$(ConvertTo-MsysPath $dataDir)`""
    "appendonly yes"
    "appendfilename `"appendonly.aof`""
    "logfile `"$(ConvertTo-MsysPath $logFile)`""
    "daemonize no"
)
[System.IO.File]::WriteAllLines(
    $configFile,
    $configLines,
    [System.Text.UTF8Encoding]::new($false)
)

$isRunning = Test-RedisReady

switch ($Action) {
    "start" {
        if ($isRunning) {
            Write-Host "Redis is already running."
            exit 0
        }
        Start-Process `
            -FilePath $redisServer `
            -ArgumentList "`"$(ConvertTo-MsysPath $configFile)`"" `
            -WorkingDirectory $redisRoot `
            -WindowStyle Hidden
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            Start-Sleep -Milliseconds 100
            if (Test-RedisReady) {
                Write-Host "Redis started."
                exit 0
            }
        }
        throw "Redis did not become ready on 127.0.0.1:$Port."
    }
    "stop" {
        if (-not $isRunning) {
            Write-Host "Redis is already stopped."
            exit 0
        }
        & $redisCli -h 127.0.0.1 -p $Port shutdown
        exit $LASTEXITCODE
    }
    "status" {
        if ($isRunning) {
            Write-Host "Redis is running."
            exit 0
        }
        Write-Host "Redis is stopped."
        exit 1
    }
}
