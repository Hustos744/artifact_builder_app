$ErrorActionPreference = "Continue"

param(
    [Parameter(Mandatory = $true)]
    [string]$BrokerDir
)

function Ensure-Directory {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Write-ReadyFile {
    param(
        [string]$ReadyPath,
        [datetime]$StartedAt
    )

    $payload = @{
        pid = $PID
        started_at = $StartedAt.ToString("o")
        heartbeat_at = (Get-Date).ToString("o")
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -Path $ReadyPath -Encoding UTF8
}

function Invoke-ConsoleCommand {
    param(
        [hashtable]$Request
    )

    $previousLocation = Get-Location
    try {
        if ($Request.cwd) {
            Set-Location $Request.cwd
        }
        & $Request.exe_path @($Request.args)
        $exitCode = if ($LASTEXITCODE -ne $null) { [int]$LASTEXITCODE } else { 0 }
        return @{
            exit_code = $exitCode
            stdout = ""
            stderr = ""
        }
    } finally {
        Set-Location $previousLocation
    }
}

function Invoke-RedirectedCommand {
    param(
        [hashtable]$Request
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Request.exe_path
    $psi.Arguments = [string]$Request.args_cmdline
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($Request.cwd) {
        $psi.WorkingDirectory = $Request.cwd
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    return @{
        exit_code = [int]$process.ExitCode
        stdout = [string]$stdout
        stderr = [string]$stderr
    }
}

$requestDir = Join-Path $BrokerDir "requests"
$resultDir = Join-Path $BrokerDir "results"
$readyPath = Join-Path $BrokerDir "ready.json"
$startedAt = Get-Date

Ensure-Directory $BrokerDir
Ensure-Directory $requestDir
Ensure-Directory $resultDir
Write-ReadyFile -ReadyPath $readyPath -StartedAt $startedAt

while ($true) {
    Write-ReadyFile -ReadyPath $readyPath -StartedAt $startedAt

    $requestFiles = @(Get-ChildItem -Path $requestDir -Filter "*.json" -ErrorAction SilentlyContinue | Sort-Object Name)
    foreach ($requestFile in $requestFiles) {
        $resultPayload = @{
            id = [System.IO.Path]::GetFileNameWithoutExtension($requestFile.Name)
            exit_code = 1
            stdout = ""
            stderr = ""
        }

        try {
            $request = Get-Content -Path $requestFile.FullName -Raw | ConvertFrom-Json -AsHashtable
            if ($request.requires_console) {
                $commandResult = Invoke-ConsoleCommand -Request $request
            } else {
                $commandResult = Invoke-RedirectedCommand -Request $request
            }
            $resultPayload.exit_code = $commandResult.exit_code
            $resultPayload.stdout = $commandResult.stdout
            $resultPayload.stderr = $commandResult.stderr
        } catch {
            $resultPayload.stderr = $_ | Out-String
            $resultPayload.exit_code = 1
        }

        $resultPath = Join-Path $resultDir ($resultPayload.id + ".json")
        $resultPayload | ConvertTo-Json -Depth 6 | Set-Content -Path $resultPath -Encoding UTF8
        Remove-Item -Path $requestFile.FullName -Force -ErrorAction SilentlyContinue
    }

    Start-Sleep -Milliseconds 300
}
