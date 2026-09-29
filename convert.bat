@echo off
rem ===========================================================================
rem  RPG Maker MV/MZ -> Android APK - the CONSOLE version (question by question).
rem  Double-click it, or run with options, e.g.:
rem     convert.bat --game "D:\Games\My Game" --install
rem     convert.bat --check        (only show which tools are installed)
rem     convert.bat --help
rem  Window version: gui.bat
rem
rem  ASCII only (legacy code page before chcp); goto instead of ( ) blocks.
rem ===========================================================================
chcp 65001 >nul 2>&1
setlocal EnableExtensions

pushd "%~dp0" 2>nul || goto :no_pushd
set "RPGM_APK_DIR=%CD%"
if "%RPGM_APK_DIR:~-1%"=="\" set "RPGM_APK_DIR=%RPGM_APK_DIR:~0,-1%"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "CLI=%RPGM_APK_DIR%\cli.py"
if not exist "%CLI%" goto :no_files

call "%RPGM_APK_DIR%\find_python.bat"
if not defined PY goto :no_python

%PY% "%CLI%" %*
set "RC=%ERRORLEVEL%"
rem 0 = success, 10 = the program already showed the error and waited for Enter.
rem Any other code (1 from an uncaught Python exception...) must keep the window.
if "%RC%"=="0" goto :ok
if "%RC%"=="10" goto :ok
echo.
echo [ERROR] The program exited with code %RC%.
echo The message above is needed to fix the problem. Folder: %RPGM_APK_DIR%
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
echo and run convert.bat from there. Folder: %RPGM_APK_DIR%
goto :pause_exit

:no_python
echo.
echo [ERROR] Could not prepare Python 3.10+.
echo Install Python from https://www.python.org/downloads/ (tick "Add python.exe
echo to PATH"), then run this file again.
goto :pause_exit

:pause_exit
echo.
pause
popd
endlocal
exit /b 1
