@echo off
rem ===========================================================================
rem  RPG Maker MV/MZ -> Android APK - the WINDOW version. Double-click it.
rem  Console version: convert.bat
rem
rem  ASCII only: cmd starts in a legacy code page and anything else would be
rem  garbled before chcp runs. No %VARIABLES% set and read inside one ( )
rem  block - cmd expands the whole block at parse time - hence goto. No for /f
rem  over %PY% either: a path with ( ) such as "Downloads (2)" breaks it.
rem ===========================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions

rem pushd, not "cd /d": on a UNC network path cd /d leaves cwd in C:\Windows
pushd "%~dp0" 2>nul || goto :no_pushd
set "RPGM_APK_DIR=%CD%"
if "%RPGM_APK_DIR:~-1%"=="\" set "RPGM_APK_DIR=%RPGM_APK_DIR:~0,-1%"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "GUI=%RPGM_APK_DIR%\gui.py"
if not exist "%GUI%" goto :no_files

call "%RPGM_APK_DIR%\find_python.bat" tk
if not defined PY goto :no_python

rem --detach: gui.py restarts itself with pythonw.exe (no black console behind
rem the window) and returns at once. The app shows its own errors (message box
rem + rpgm-to-apk.log). Without pythonw it simply runs here in the console.
%PY% "%GUI%" --detach %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :ok
if "%RC%"=="10" goto :ok
echo.
echo [ERROR] The program exited with code %RC%.
echo Details are in rpgm-to-apk.log in: %RPGM_APK_DIR%
goto :pause_exit

:ok
popd
endlocal
exit /b 0

:no_pushd
echo [ERROR] Cannot enter the program folder:
echo         %~dp0
echo Copy the WHOLE folder to a local disk (e.g. the Desktop) and run it from there.
echo.
pause
exit /b 1

:no_files
echo.
echo [ERROR] Program files not found. Unpack the WHOLE archive into one folder
echo and run gui.bat from there. Folder: %RPGM_APK_DIR%
goto :pause_exit

:no_python
echo.
echo [ERROR] Could not prepare Python 3.10+ with tkinter.
echo Install Python from https://www.python.org/downloads/ (tick "Add python.exe
echo to PATH" and keep "tcl/tk and IDLE"), then run this file again.
echo You can also try the console version: convert.bat
goto :pause_exit

:pause_exit
echo.
pause
popd
endlocal
exit /b 1
