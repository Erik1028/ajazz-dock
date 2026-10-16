# Build Hexpad into dist\Hexpad\ (Hexpad.exe and its files; no Python needed to run).
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$vpy = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
& $vpy -m pip install --quiet --upgrade pyinstaller

Write-Host "Generating app icon..." -ForegroundColor Cyan
& $vpy "$root\tools\make_icon.py"

Write-Host "Building Hexpad.exe..." -ForegroundColor Cyan
& $vpy -m PyInstaller --noconfirm --clean --onedir --windowed --name Hexpad `
  --icon "$root\assets\hexpad.ico" `
  --uac-admin `
  --add-data "$root\dock\native\LibreHardwareMonitorLib.dll;." `
  --add-data "$root\dock\native\System.Memory.dll;." `
  --add-data "$root\dock\native\System.Runtime.CompilerServices.Unsafe.dll;." `
  --add-data "$root\dock\native\System.Numerics.Vectors.dll;." `
  --add-data "$root\dock\native\System.Buffers.dll;." `
  --collect-all hid `
  --collect-all pycaw `
  --collect-all comtypes `
  --collect-submodules keyboard `
  --collect-submodules mouse `
  --collect-all sounddevice `
  --collect-all soundfile `
  --collect-all psutil `
  --collect-all kasa `
  --collect-all pythonnet `
  --collect-all clr_loader `
  --collect-all winrt `
  --collect-submodules winrt `
  --collect-all websocket `
  --exclude-module tkinter `
  --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
  --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtMultimedia `
  --distpath "$root\dist" --workpath "$root\build" --specpath "$root" `
  "$root\run.py"

if (Test-Path "$root\dist\Hexpad\Hexpad.exe") {
  Write-Host "`nBuilt: $root\dist\Hexpad\Hexpad.exe (onedir folder - no per-launch unpack, faster startup)" -ForegroundColor Green
} else {
  Write-Host "`nBuild failed - see PyInstaller output above." -ForegroundColor Red
}
