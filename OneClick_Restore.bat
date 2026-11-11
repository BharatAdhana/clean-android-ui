@echo off
setlocal enabledelayedexpansion
pushd "%~dp0" >nul

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 --version >nul 2>nul
    if !errorlevel!==0 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Python 3.9+ was not found on PATH.
    echo Install Python from https://www.python.org/downloads/ and re-run this script.
    set "EXIT_CODE=1"
    goto maybe_pause
)

echo ========================================================================
echo   Clean MIUI AI - One-Click Restore
echo   Restores packages, settings, appops, and display density from backup.
echo ========================================================================
%PY_CMD% scripts\restore.py %*
set "EXIT_CODE=%errorlevel%"

if %EXIT_CODE%==0 (
    echo.
    echo [DONE] Restore script completed. Review report inside your backups\ folder.
) else (
    echo.
    echo [ERROR] Restore exited with code %EXIT_CODE%. Check device connection and permissions.
)

:maybe_pause
popd >nul
echo %cmdcmdline% | find /i "%~f0" >nul
if %errorlevel%==0 (
    if /i not "%CLEAN_MIUI_NO_PAUSE%"=="1" pause
)
exit /b %EXIT_CODE%
