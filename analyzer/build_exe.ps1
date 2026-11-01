<#
.SYNOPSIS
Bundles the analyzer into latency-analyzer.exe with PyInstaller, so hosts can analyse Latency Cam uploads without
Python.

.DESCRIPTION
Creates a virtual environment in the build folder (so the system's Python packages are left alone), installs the
analyzer's requirements and PyInstaller into it, and writes BuildDir\latency-analyzer.exe. Needs Python 3 with the
py launcher, and network access the first time.

.PARAMETER BuildDir
Folder for the virtual environment, PyInstaller's work files and the finished exe.
#>
param(
    [Parameter(Mandatory)] [string]$BuildDir
)
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)"
    }
}

$here = $PSScriptRoot
$BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
$venv = Join-Path $BuildDir "analyzer-venv"
$python = Join-Path $venv "Scripts\python.exe"
$work = Join-Path $BuildDir "pyinstaller"

if (-not (Test-Path $python)) {
    Write-Host "Creating a virtual environment in $venv"
    Invoke-Checked "Creating the virtual environment" { py -3 -m venv $venv }
}
Invoke-Checked "Installing the requirements" { & $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $here "requirements.txt") pyinstaller }

Write-Host "Bundling latency-analyzer.exe"
# camera_mode is imported only when a Latency Cam recording is analysed, so name it for PyInstaller
Invoke-Checked "PyInstaller" {
    & $python -m PyInstaller --noconfirm --log-level WARN --onefile --console --name latency-analyzer `
        --distpath $BuildDir --workpath $work --specpath $work `
        --paths $here --hidden-import camera_mode --hidden-import camera_timing --hidden-import traces `
        (Join-Path $here "analyze_video.py")
}
Write-Host "Wrote $(Join-Path $BuildDir 'latency-analyzer.exe')"
