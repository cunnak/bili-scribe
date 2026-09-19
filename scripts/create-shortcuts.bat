@echo off
REM Create Desktop shortcuts for bili-scribe.
REM ASCII-only: Chinese literals inside .bat/.ps1 break on some locales.
setlocal
for %%I in ("%~dp0..") do set "APPDIR=%%~fI"

if not exist "%APPDIR%\start.bat" (
  echo [X] start.bat not found in %APPDIR%
  pause
  exit /b 1
)

REM Pass the path through the environment instead of string-splicing it into
REM the PowerShell command line. Splicing breaks on folders containing spaces.
set "APPDIR_VALUE=%APPDIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "$d = [Environment]::GetFolderPath('Desktop');" ^
  "$a = $env:APPDIR_VALUE;" ^
  "$s1 = $w.CreateShortcut($d + '\bili-scribe.lnk');" ^
  "$s1.TargetPath = $a + '\start-silent.vbs';" ^
  "$s1.WorkingDirectory = $a;" ^
  "$s1.Description = 'bili-scribe - B站视频总结 (no console)';" ^
  "$s1.Save();" ^
  "$s2 = $w.CreateShortcut($d + '\bili-scribe-debug.lnk');" ^
  "$s2.TargetPath = $a + '\start.bat';" ^
  "$s2.WorkingDirectory = $a;" ^
  "$s2.Description = 'bili-scribe - B站视频总结 (with console, for troubleshooting)';" ^
  "$s2.Save();" ^
  "Write-Host ('Desktop: ' + $d)"

echo.
echo Done. Two shortcuts were created on your Desktop:
echo   bili-scribe.lnk        - silent start
echo   bili-scribe-debug.lnk  - start with a console window
echo.
echo You can rename them freely.
pause
