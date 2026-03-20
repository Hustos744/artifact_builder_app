# Local Setup

This setup path is intended for a low-resource Windows VM without Docker.

## What `bootstrap.ps1` does

The script [bootstrap.ps1](/c:/Users/Max/.gemini/antigravity/brain/cbf59891-bf60-47c6-a5ec-67aeaf492538/DFIR_App/bootstrap.ps1):

- finds or installs a local `Python 3.12`
- creates `.venv` for the app
- installs the app Python dependencies
- downloads and prepares:
  - `7-Zip`
  - Zimmerman tools
  - `Hayabusa`
  - `Takajo`
  - `srum-dump` source
- installs Python dependencies for `srum-dump`
- creates or updates `settings.json`
- prepares `workspace`

## Requirements

- Windows
- Internet access

You no longer need to preinstall Python manually. If `Python 3.12` is not found, bootstrap installs it locally into `runtime\Python312`.

## Run

```powershell
cd DFIR_App
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
powershell -ExecutionPolicy Bypass -File .\run_app.ps1
```

## Files created by bootstrap

- `.venv`
- `tools`
- `runtime`
- `workspace`
- `settings.json`

## Optional overrides

To force a specific Python for `srum-dump`:

```powershell
$env:DFIR_SRUM_PYTHON_OVERRIDE = "C:\Python312\python.exe"
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

To force a specific Python for the app UI:

```powershell
$env:DFIR_APP_PYTHON_OVERRIDE = "C:\Python310\python.exe"
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```
