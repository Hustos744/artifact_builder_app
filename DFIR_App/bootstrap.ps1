$ErrorActionPreference = "Stop"

$script:BundledPythonVersion = "3.12.10"
$script:BundledPythonInstallerUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

function Write-Step {
    param([string]$Message)
    Write-Host "[bootstrap] $Message"
}

function Ensure-Directory {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Download-File {
    param(
        [string]$Url,
        [string]$Destination
    )

    Ensure-Directory (Split-Path -Parent $Destination)
    Write-Step "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
}

function Expand-ZipClean {
    param(
        [string]$ZipPath,
        [string]$Destination
    )

    if (Test-Path $Destination) {
        Remove-Item $Destination -Recurse -Force
    }
    Ensure-Directory $Destination
    Expand-Archive -Path $ZipPath -DestinationPath $Destination -Force
}

function Get-FileHashString {
    param([string]$Path)

    if (-not (Test-Path $Path -PathType Leaf)) {
        return ""
    }

    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Install-PythonRequirements {
    param(
        [string]$PythonExe,
        [string]$RequirementsPath,
        [string]$MarkerPath,
        [string]$Label
    )

    $requirementsHash = Get-FileHashString -Path $RequirementsPath
    $installedHash = ""
    if (Test-Path $MarkerPath -PathType Leaf) {
        $installedHash = (Get-Content $MarkerPath -Raw).Trim().ToLowerInvariant()
    }

    if ($requirementsHash -and $requirementsHash -eq $installedHash) {
        Write-Step "$Label already installed, skipping"
        return
    }

    Write-Step "Installing $Label"
    & $PythonExe -m pip install --upgrade pip
    & $PythonExe -m pip install -r $RequirementsPath
    $requirementsHash | Set-Content -Path $MarkerPath -Encoding ASCII
}

function Ensure-PythonModule {
    param(
        [string]$PythonExe,
        [string]$ImportName,
        [string]$PackageName
    )

    $checkCode = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ImportName') else 1)"
    & $PythonExe -c $checkCode 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Step "Python module $ImportName already installed, skipping"
        return
    }

    Write-Step "Installing missing Python package $PackageName"
    & $PythonExe -m pip install $PackageName
}

function Get-GitHubLatestAssetUrl {
    param(
        [string]$Repo,
        [string]$NameRegex
    )

    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "dfir-artifact-parser" }
    $asset = $release.assets | Where-Object { $_.name -match $NameRegex } | Select-Object -First 1
    if (-not $asset) {
        throw "Unable to find asset for $Repo matching $NameRegex"
    }
    return $asset.browser_download_url
}

function Find-FirstFile {
    param(
        [string]$Root,
        [string]$Pattern
    )

    $match = Get-ChildItem -Path $Root -Recurse -File -Filter $Pattern -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($match) {
        return $match.FullName
    }
    return ""
}

function Get-BundledPythonExe {
    param([string]$RepoRoot)

    return (Join-Path $RepoRoot "runtime\Python312\python.exe")
}

function Install-LocalPython312 {
    param(
        [string]$RepoRoot,
        [string]$DownloadsDir
    )

    $pythonExe = Get-BundledPythonExe -RepoRoot $RepoRoot
    if (Test-Path $pythonExe -PathType Leaf) {
        Write-Step "Local Python $($script:BundledPythonVersion) already present, skipping"
        return $pythonExe
    }

    $targetDir = Split-Path -Parent $pythonExe
    $installerPath = Join-Path $DownloadsDir "python-$($script:BundledPythonVersion)-amd64.exe"

    if (-not (Test-Path $installerPath -PathType Leaf)) {
        Download-File -Url $script:BundledPythonInstallerUrl -Destination $installerPath
    }

    Ensure-Directory $targetDir
    Write-Step "Installing local Python $($script:BundledPythonVersion) to $targetDir"
    $process = Start-Process -FilePath $installerPath -ArgumentList @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=$targetDir",
        "PrependPath=0",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "AssociateFiles=0",
        "Shortcuts=0",
        "Include_test=0",
        "SimpleInstall=0"
    ) -Wait -PassThru

    if ($process.ExitCode -ne 0) {
        throw "Python installer exited with code $($process.ExitCode)"
    }

    if (-not (Test-Path $pythonExe -PathType Leaf)) {
        throw "Local Python installation completed but python.exe was not found at $pythonExe"
    }

    return $pythonExe
}

function Resolve-Python312 {
    param(
        [string]$RepoRoot,
        [string]$DownloadsDir
    )

    $candidates = @()

    if ($env:DFIR_SRUM_PYTHON_OVERRIDE) {
        $candidates += $env:DFIR_SRUM_PYTHON_OVERRIDE
    }

    $candidates += Get-BundledPythonExe -RepoRoot $RepoRoot

    $commonPaths = @(
        "C:\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    $candidates += $commonPaths

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate -PathType Leaf)) {
            Write-Step "Using existing Python 3.12: $candidate"
            return $candidate
        }
    }

    try {
        $resolved = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            $resolvedPath = ($resolved | Select-Object -First 1).Trim()
            Write-Step "Using Python 3.12 from launcher: $resolvedPath"
            return $resolvedPath
        }
    } catch {
    }

    return (Install-LocalPython312 -RepoRoot $RepoRoot -DownloadsDir $DownloadsDir)
}

function Resolve-AppPython {
    param(
        [string]$RepoRoot,
        [string]$FallbackPythonExe
    )

    $candidates = @()

    if ($env:DFIR_APP_PYTHON_OVERRIDE) {
        $candidates += $env:DFIR_APP_PYTHON_OVERRIDE
    }

    if ($FallbackPythonExe) {
        $candidates += $FallbackPythonExe
    }

    $candidates += Get-BundledPythonExe -RepoRoot $RepoRoot

    $commonPaths = @(
        "C:\Python310\python.exe",
        "C:\Python311\python.exe",
        "C:\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    $candidates += $commonPaths

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate -PathType Leaf)) {
            Write-Step "Using app Python: $candidate"
            return $candidate
        }
    }

    try {
        $resolved = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            $resolvedPath = ($resolved | Select-Object -First 1).Trim()
            Write-Step "Using app Python from launcher: $resolvedPath"
            return $resolvedPath
        }
    } catch {
    }

    throw "Python for app runtime not found. Bootstrap could not resolve a usable interpreter."
}

function Ensure-AppVenv {
    param(
        [string]$RepoRoot,
        [string]$PythonExe
    )

    $venvDir = Join-Path $RepoRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (-not (Test-Path $venvPython)) {
        Write-Step "Creating app virtual environment"
        & $PythonExe -m venv $venvDir
    } else {
        Write-Step "App virtual environment already exists, skipping creation"
    }

    Install-PythonRequirements `
        -PythonExe $venvPython `
        -RequirementsPath (Join-Path $RepoRoot "requirements.txt") `
        -MarkerPath (Join-Path $venvDir ".requirements.sha256") `
        -Label "app requirements"

    return $venvPython
}

function Install-SrumSource {
    param(
        [string]$ToolsRoot,
        [string]$PythonExe
    )

    $targetDir = Join-Path $ToolsRoot "srum-dump"
    $scriptPath = Join-Path $targetDir "srum-dump\srum_dump.py"
    if (-not (Test-Path $scriptPath)) {
        $zipPath = Join-Path $ToolsRoot "_downloads\srum-dump.zip"
        Download-File -Url "https://codeload.github.com/MarkBaggett/srum-dump/zip/refs/heads/master" -Destination $zipPath
        $extractRoot = Join-Path $ToolsRoot "_downloads\srum-dump-src"
        Expand-ZipClean -ZipPath $zipPath -Destination $extractRoot
        $expanded = Get-ChildItem -Path $extractRoot -Directory | Select-Object -First 1
        if (-not $expanded) {
            throw "Unable to extract srum-dump source"
        }
        if (Test-Path $targetDir) {
            Remove-Item $targetDir -Recurse -Force
        }
        Move-Item -Path $expanded.FullName -Destination $targetDir
    } else {
        Write-Step "srum-dump source already exists, skipping download"
    }

    Install-PythonRequirements `
        -PythonExe $PythonExe `
        -RequirementsPath (Join-Path $targetDir "requirements.txt") `
        -MarkerPath (Join-Path $targetDir ".requirements.sha256") `
        -Label "srum-dump Python dependencies"

    Ensure-PythonModule -PythonExe $PythonExe -ImportName "Registry" -PackageName "python-registry"
    Ensure-PythonModule -PythonExe $PythonExe -ImportName "yaml" -PackageName "pyyaml"

    return $targetDir
}

function Install-Hayabusa {
    param([string]$ToolsRoot)

    $targetDir = Join-Path $ToolsRoot "Hayabusa"
    $exePath = Find-FirstFile -Root $targetDir -Pattern "hayabusa*.exe"
    if (-not $exePath) {
        $zipPath = Join-Path $ToolsRoot "_downloads\hayabusa.zip"
        $url = Get-GitHubLatestAssetUrl -Repo "Yamato-Security/hayabusa" -NameRegex "hayabusa-.*win-x64.*\.zip$"
        Download-File -Url $url -Destination $zipPath
        Expand-ZipClean -ZipPath $zipPath -Destination $targetDir
        $exePath = Find-FirstFile -Root $targetDir -Pattern "hayabusa*.exe"
    } else {
        Write-Step "Hayabusa already installed, skipping download"
    }
    if (-not $exePath) {
        throw "Hayabusa executable not found after installation"
    }
    return $exePath
}

function Install-Takajo {
    param([string]$ToolsRoot)

    $targetDir = Join-Path $ToolsRoot "takajo"
    $exePath = Find-FirstFile -Root $targetDir -Pattern "takajo*.exe"
    if (-not $exePath) {
        $zipPath = Join-Path $ToolsRoot "_downloads\takajo.zip"
        $url = Get-GitHubLatestAssetUrl -Repo "Yamato-Security/takajo" -NameRegex "takajo-.*win-x64.*\.zip$"
        Download-File -Url $url -Destination $zipPath
        Expand-ZipClean -ZipPath $zipPath -Destination $targetDir
        $exePath = Find-FirstFile -Root $targetDir -Pattern "takajo*.exe"
    } else {
        Write-Step "Takajo already installed, skipping download"
    }
    if (-not $exePath) {
        throw "Takajo executable not found after installation"
    }
    return $exePath
}

function Install-ZimmermanTools {
    param([string]$ToolsRoot)

    $targetDir = Join-Path $ToolsRoot "Zimmerman"
    $required = @("MFTECmd.exe", "AmcacheParser.exe", "AppCompatCacheParser.exe", "PECmd.exe")
    $missing = @()
    foreach ($fileName in $required) {
        if (-not (Find-FirstFile -Root $targetDir -Pattern $fileName)) {
            $missing += $fileName
        }
    }

    if ($missing.Count -gt 0) {
        Ensure-Directory $targetDir
        $scriptPath = Join-Path $ToolsRoot "_downloads\Get-ZimmermanTools.ps1"
        Download-File -Url "https://raw.githubusercontent.com/EricZimmerman/Get-ZimmermanTools/master/Get-ZimmermanTools.ps1" -Destination $scriptPath
        Write-Step "Installing Zimmerman tools via Get-ZimmermanTools.ps1"
        & powershell -NoLogo -ExecutionPolicy Bypass -File $scriptPath -Dest $targetDir -NetVersion 9
    } else {
        Write-Step "Zimmerman tools already installed, skipping download"
    }

    return @{
        "MFTECmd" = Find-FirstFile -Root $targetDir -Pattern "MFTECmd.exe"
        "AmcacheParser" = Find-FirstFile -Root $targetDir -Pattern "AmcacheParser.exe"
        "AppCompatCacheParser" = Find-FirstFile -Root $targetDir -Pattern "AppCompatCacheParser.exe"
        "PECmd" = Find-FirstFile -Root $targetDir -Pattern "PECmd.exe"
    }
}

function Install-7Zip {
    param([string]$ToolsRoot)

    $targetDir = Join-Path $ToolsRoot "7-Zip"
    $exePath = Join-Path $targetDir "7z.exe"
    if (-not (Test-Path $exePath)) {
        $downloadPage = Invoke-WebRequest -Uri "https://www.7-zip.org/download.html" -UseBasicParsing
        $match = [regex]::Match($downloadPage.Content, 'href="(?<path>a/7z\d+-x64\.exe)"')
        if (-not $match.Success) {
            throw "Unable to find 7-Zip x64 installer URL"
        }
        $installerUrl = "https://www.7-zip.org/" + $match.Groups["path"].Value
        $installerPath = Join-Path $ToolsRoot "_downloads\7zip-installer.exe"
        Download-File -Url $installerUrl -Destination $installerPath
        Ensure-Directory $targetDir
        Start-Process -FilePath $installerPath -ArgumentList "/S", "/D=$targetDir" -Wait
    } else {
        Write-Step "7-Zip already installed, skipping download"
    }
    return $exePath
}

function Resolve-InstalledToolPaths {
    param(
        [string]$RepoRoot,
        [string]$ToolsRoot,
        [string]$SrumPython
    )

    $resolvedWorkspace = Join-Path $RepoRoot "workspace"
    $resolvedPaths = @{
        "7z" = ""
        "MFTECmd" = ""
        "AmcacheParser" = ""
        "AppCompatCacheParser" = ""
        "PECmd" = ""
        "Hayabusa" = ""
        "Takajo" = ""
        "SRUMDumpSourceDir" = ""
        "SRUMDumpPython" = ""
        "output_dir" = $resolvedWorkspace
    }

    $sevenZipExe = Join-Path $ToolsRoot "7-Zip\7z.exe"
    if (Test-Path $sevenZipExe -PathType Leaf) {
        $resolvedPaths["7z"] = $sevenZipExe
    }

    $zimmermanRoot = Join-Path $ToolsRoot "Zimmerman"
    if (Test-Path $zimmermanRoot -PathType Container) {
        $resolvedPaths["MFTECmd"] = Find-FirstFile -Root $zimmermanRoot -Pattern "MFTECmd.exe"
        $resolvedPaths["AmcacheParser"] = Find-FirstFile -Root $zimmermanRoot -Pattern "AmcacheParser.exe"
        $resolvedPaths["AppCompatCacheParser"] = Find-FirstFile -Root $zimmermanRoot -Pattern "AppCompatCacheParser.exe"
        $resolvedPaths["PECmd"] = Find-FirstFile -Root $zimmermanRoot -Pattern "PECmd.exe"
    }

    $hayabusaRoot = Join-Path $ToolsRoot "Hayabusa"
    if (Test-Path $hayabusaRoot -PathType Container) {
        $resolvedPaths["Hayabusa"] = Find-FirstFile -Root $hayabusaRoot -Pattern "hayabusa*.exe"
    }

    $takajoRoot = Join-Path $ToolsRoot "takajo"
    if (Test-Path $takajoRoot -PathType Container) {
        $resolvedPaths["Takajo"] = Find-FirstFile -Root $takajoRoot -Pattern "takajo*.exe"
    }

    $srumSourceRoot = Join-Path $ToolsRoot "srum-dump"
    if (Test-Path $srumSourceRoot -PathType Container) {
        $resolvedPaths["SRUMDumpSourceDir"] = $srumSourceRoot
    }

    if ($SrumPython -and (Test-Path $SrumPython -PathType Leaf)) {
        $resolvedPaths["SRUMDumpPython"] = $SrumPython
    } else {
        $bundledPython = Get-BundledPythonExe -RepoRoot $RepoRoot
        if (Test-Path $bundledPython -PathType Leaf) {
            $resolvedPaths["SRUMDumpPython"] = $bundledPython
        }
    }

    return $resolvedPaths
}

function Update-SettingsFile {
    param(
        [string]$SettingsPath,
        [hashtable]$ToolPaths,
        [string]$OutputDir
    )

    $settings = @{}
    if (Test-Path $SettingsPath) {
        $raw = Get-Content $SettingsPath -Raw
        if ($raw.Trim()) {
            try {
                $settings = $raw | ConvertFrom-Json -AsHashtable
            } catch {
                $settings = @{}
            }
        }
    }

    $settings.Remove("SRUMDumpConfig") | Out-Null
    $settings.Remove("SRUMDump") | Out-Null

    foreach ($key in $ToolPaths.Keys) {
        if ($ToolPaths[$key]) {
            $settings[$key] = $ToolPaths[$key]
        }
    }

    $settings["output_dir"] = $OutputDir

    $artifactOptions = $settings["artifact_options"]
    $hasValidArtifactOptions = $artifactOptions -is [hashtable]
    if ($hasValidArtifactOptions) {
        foreach ($artifactKey in @("parse_mft", "parse_amcache", "parse_shimcache", "parse_prefetch", "parse_hayabusa", "parse_srum")) {
            if (-not $artifactOptions.ContainsKey($artifactKey) -or $artifactOptions[$artifactKey] -isnot [bool]) {
                $hasValidArtifactOptions = $false
                break
            }
        }
    }

    if (-not $hasValidArtifactOptions) {
        $settings["artifact_options"] = @{
            "parse_mft" = $true
            "parse_amcache" = $true
            "parse_shimcache" = $true
            "parse_prefetch" = $true
            "parse_hayabusa" = $true
            "parse_srum" = $true
        }
    }

    $settings | ConvertTo-Json -Depth 10 | Set-Content -Path $SettingsPath -Encoding UTF8
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolsRoot = Join-Path $repoRoot "tools"
$runtimeRoot = Join-Path $repoRoot "runtime"
$workspaceDir = Join-Path $repoRoot "workspace"
$settingsPath = Join-Path $repoRoot "settings.json"
$downloadsDir = Join-Path $toolsRoot "_downloads"

Ensure-Directory $toolsRoot
Ensure-Directory $runtimeRoot
Ensure-Directory $workspaceDir
Ensure-Directory $downloadsDir

$srumPython = Resolve-Python312 -RepoRoot $repoRoot -DownloadsDir $downloadsDir
$appPython = Resolve-AppPython -RepoRoot $repoRoot -FallbackPythonExe $srumPython
$venvPython = Ensure-AppVenv -RepoRoot $repoRoot -PythonExe $appPython

$sevenZipPath = Install-7Zip -ToolsRoot $toolsRoot
$zimmermanTools = Install-ZimmermanTools -ToolsRoot $toolsRoot
$hayabusaPath = Install-Hayabusa -ToolsRoot $toolsRoot
$takajoPath = Install-Takajo -ToolsRoot $toolsRoot
$srumSourceDir = Install-SrumSource -ToolsRoot $toolsRoot -PythonExe $srumPython

$toolPaths = Resolve-InstalledToolPaths -RepoRoot $repoRoot -ToolsRoot $toolsRoot -SrumPython $srumPython
Update-SettingsFile -SettingsPath $settingsPath -ToolPaths $toolPaths -OutputDir $toolPaths["output_dir"]
Write-Step "Synchronized settings.json with installed tool paths"

$manifest = @{
    "app_python" = $appPython
    "venv_python" = $venvPython
    "srum_python" = $srumPython
    "settings_file" = $settingsPath
    "workspace" = $workspaceDir
    "tools" = $toolPaths
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -Path (Join-Path $toolsRoot "bootstrap-manifest.json") -Encoding UTF8

Write-Step "Bootstrap complete"
Write-Step "Run .\\run_app.ps1 to start the application"
