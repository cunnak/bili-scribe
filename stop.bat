@echo off
REM Stop bili-scribe: kill whatever is listening on the port.
REM
REM Usage:  stop.bat            stop on the default port 8818
REM         stop.bat 8819       stop on a custom port
REM         stop.bat 8818 quiet  do not wait for a keypress (for scripting)
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8818"

echo Stopping bili-scribe on port %PORT% ...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue;" ^
  "if (-not $c) { Write-Host 'Nothing is listening on port %PORT%. Already stopped.'; exit 0 };" ^
  "$c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {" ^
  "  Write-Host ('Stopping PID ' + $_);" ^
  "  Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue" ^
  "};" ^
  "Start-Sleep -Milliseconds 800;" ^
  "$left = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue;" ^
  "if ($left) { Write-Host 'Port is still in use.' } else { Write-Host 'Port %PORT% is now free.' }"

if "%~2"=="" pause
