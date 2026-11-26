$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    & $python -m pip install -r requirements.txt 'pyinstaller==6.22.3'
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $python -m unittest -v test_bridge
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & $python -m PyInstaller --noconfirm --onefile --windowed --name WristLink --icon wristlink.ico --add-data 'wristlink.ico;.' --exclude-module _ssl --exclude-module _hashlib --collect-submodules winrt --collect-submodules bleak.backends.winrt wristlink.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
