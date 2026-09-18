$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -e ".[desktop,dev]"
& .\.venv\Scripts\python.exe main.py
