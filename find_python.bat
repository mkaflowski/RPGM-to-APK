@echo off
rem ===========================================================================
rem  Helper for gui.bat and convert.bat (called with "call", do not run it alone).
rem
rem  Sets PY to a Python 3.10+ command: the portable one from a previous run
rem  (.python\), then "py -3", "python", "python3". With the argument "tk" the
rem  Python must also have tkinter (the window needs it).
rem  When nothing fits, downloads a PORTABLE Python into .python\ of this folder
rem  (python-build-standalone, ~30 MB, includes tkinter). Nothing is installed
rem  in the system, no admin rights - deleting the folder removes it.
rem
rem  No setlocal here: PY has to reach the caller. PY keeps the WHOLE command,
rem  so a path already has its quotes inside the value ("py -3" is two words).
rem  ASCII only: cmd starts in a legacy code page.
rem ===========================================================================
set "PY="
set "PYCHECK=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if /i "%~1"=="tk" set "PYCHECK=import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)"

if not exist "%RPGM_APK_DIR%\.python\python.exe" goto :try_launcher
"%RPGM_APK_DIR%\.python\python.exe" -c "%PYCHECK%" >nul 2>&1
if not errorlevel 1 set PY="%RPGM_APK_DIR%\.python\python.exe"
if defined PY goto :eof

:try_launcher
py -3 -c "%PYCHECK%" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :eof
python -c "%PYCHECK%" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :eof
python3 -c "%PYCHECK%" >nul 2>&1
if not errorlevel 1 set "PY=python3"
if defined PY goto :eof

echo.
echo No suitable Python 3.10+ found - downloading a portable one
echo (~30 MB, once, only into this folder)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%RPGM_APK_DIR%\bootstrap_python.ps1" -Dest "%RPGM_APK_DIR%\.python"
if not exist "%RPGM_APK_DIR%\.python\python.exe" goto :eof
"%RPGM_APK_DIR%\.python\python.exe" -c "%PYCHECK%" >nul 2>&1
if not errorlevel 1 set PY="%RPGM_APK_DIR%\.python\python.exe"
goto :eof
