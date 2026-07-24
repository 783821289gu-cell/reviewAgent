param(
    [int]$PerModuleTimeoutSeconds = 180,
    [string]$Pattern = "test_*.py"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$testsRoot = Join-Path $projectRoot "backend\tests"
$pythonCommand = (Get-Command python -ErrorAction Stop).Source
$originalEnvironment = @{
    REVIEW_AGENT_LOAD_DOTENV = $env:REVIEW_AGENT_LOAD_DOTENV
    REVIEW_AGENT_LLM_MODE = $env:REVIEW_AGENT_LLM_MODE
    REVIEW_AGENT_EMBEDDING_MODE = $env:REVIEW_AGENT_EMBEDDING_MODE
}
$failedModules = [System.Collections.Generic.List[string]]::new()
$timedOutModules = [System.Collections.Generic.List[string]]::new()

try {
    $env:REVIEW_AGENT_LOAD_DOTENV = "0"
    $env:REVIEW_AGENT_LLM_MODE = "local_structured"
    $env:REVIEW_AGENT_EMBEDDING_MODE = "local_sparse"

    $testFiles = Get-ChildItem -LiteralPath $testsRoot -Filter $Pattern | Sort-Object Name
    if (-not $testFiles) {
        throw "No backend test modules matched '$Pattern'."
    }

    foreach ($testFile in $testFiles) {
        Write-Host "RUN $($testFile.Name)"
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $pythonCommand
        $startInfo.Arguments = (
            '-m unittest discover -v -s "{0}" -p "{1}"' -f
            $testsRoot,
            $testFile.Name
        )
        $startInfo.WorkingDirectory = $projectRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "Failed to start backend test module $($testFile.Name)."
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        if (-not $process.WaitForExit($PerModuleTimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            $timedOutModules.Add($testFile.Name)
            Write-Host "TIMEOUT $($testFile.Name) after ${PerModuleTimeoutSeconds}s" -ForegroundColor Red
        }
        else {
            if ($process.ExitCode -ne 0) {
                $failedModules.Add($testFile.Name)
                Write-Host "FAILED $($testFile.Name)" -ForegroundColor Red
            }
            else {
                Write-Host "PASSED $($testFile.Name)" -ForegroundColor Green
            }
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($stdout) {
            Write-Output $stdout.TrimEnd()
        }
        if ($stderr) {
            Write-Output $stderr.TrimEnd()
        }
        $process.Dispose()
    }
}
finally {
    foreach ($name in $originalEnvironment.Keys) {
        $value = $originalEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item -LiteralPath "Env:$name" -Value $value
        }
    }
}

if ($timedOutModules.Count -gt 0) {
    Write-Host "Timed out modules: $($timedOutModules -join ', ')" -ForegroundColor Red
}
if ($failedModules.Count -gt 0) {
    Write-Host "Failed modules: $($failedModules -join ', ')" -ForegroundColor Red
}
if ($timedOutModules.Count -gt 0 -or $failedModules.Count -gt 0) {
    exit 1
}

Write-Host "All matched backend test modules passed." -ForegroundColor Green
