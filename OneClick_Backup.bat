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

set "SERIAL_ARG="
set "RAW_ARGS=%*"
:parse_args
if "%~1"=="" goto run_backup
if /i "%~1"=="--serial" (
    if not "%~2"=="" set "SERIAL_ARG=--serial %~2"
    shift
    shift
    goto parse_args
)
shift
goto parse_args

:run_backup
echo ========================================================================
echo   Clean MIUI AI - One-Click Backup & Restore Verification
echo   Existing OS only (No root / No ROM flash). Checking ADB first...
echo ========================================================================
%PY_CMD% scripts\inspect_and_backup.py %RAW_ARGS%
if not %errorlevel%==0 (
    echo.
    echo [FAILED] Backup did not complete. Check docs\CONNECTION_GUIDE.md for USB or Wi-Fi ADB setup.
    set "EXIT_CODE=1"
    goto maybe_pause
)

echo.
echo ========================================================================
echo   Verifying backup integrity & dry-run restore plan...
echo ========================================================================
%PY_CMD% scripts\restore.py %SERIAL_ARG% --dry-run
set "EXIT_CODE=%errorlevel%"
if %EXIT_CODE%==0 (
    echo.
    echo [OK] Backup created and dry-run restore verified!
    echo See backups\LATEST_BACKUP.txt for your latest snapshot folder.
) else (
    echo.
    echo [WARNING] Restore verification returned code %EXIT_CODE%. Review messages above before continuing.
)

:maybe_pause
popd >nul
echo %cmdcmdline% | find /i "%~f0" >nul
if %errorlevel%==0 (
    if /i not "%CLEAN_MIUI_NO_PAUSE%"=="1" pause
)
exit /b %EXIT_CODE%
