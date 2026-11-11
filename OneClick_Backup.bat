@echo off
setlocal enabledelayedexpansion

set "SCRIPT_ROOT=%~dp0"
set "OPT_SERIAL="
set "OPT_PROJ_DIR="
set "HAS_PROJ_DIR=0"
set "BAD_PROJ_DIR=0"
set "MISSING_PROJ_VAL=0"
set "HELP_MODE=0"
set "OTHER_ARGS="

:parse_args
if [%1]==[] goto after_parse
if /i "%~1"=="--help" (
    set "HELP_MODE=1"
    set "OTHER_ARGS=!OTHER_ARGS! --help"
    shift
    goto parse_args
)
if /i "%~1"=="-h" (
    set "HELP_MODE=1"
    set "OTHER_ARGS=!OTHER_ARGS! -h"
    shift
    goto parse_args
)
if /i "%~1"=="--serial" (
    if [%2]==[] (
        set "OTHER_ARGS=!OTHER_ARGS! --serial"
        shift
        goto parse_args
    )
    set "OPT_SERIAL=%~2"
    set "OTHER_ARGS=!OTHER_ARGS! --serial "%~2""
    shift
    shift
    goto parse_args
)
if /i "%~1"=="--project-dir" (
    set "HAS_PROJ_DIR=1"
    if [%2]==[] (
        set "MISSING_PROJ_VAL=1"
        shift
        goto parse_args
    )
    set "OPT_PROJ_DIR=%~2"
    call :normalize_proj_dir
    shift
    shift
    goto parse_args
)
set "ARG_CUR=%~1"
if /i "!ARG_CUR:~0,9!"=="--serial=" (
    set "OPT_SERIAL=!ARG_CUR:~9!"
    set "OTHER_ARGS=!OTHER_ARGS! --serial "!ARG_CUR:~9!""
    shift
    goto parse_args
)
if /i "!ARG_CUR:~0,14!"=="--project-dir=" (
    set "HAS_PROJ_DIR=1"
    set "OPT_PROJ_DIR=!ARG_CUR:~14!"
    call :normalize_proj_dir
    shift
    goto parse_args
)
set "OTHER_ARGS=!OTHER_ARGS! %1"
shift
goto parse_args

:normalize_proj_dir
if "!OPT_PROJ_DIR!"=="" (
    set "BAD_PROJ_DIR=1"
    goto :eof
)
set "OPT_PROJ_CHECK=!OPT_PROJ_DIR: =!"
if "!OPT_PROJ_CHECK!"=="" (
    set "OPT_PROJ_DIR="
    set "BAD_PROJ_DIR=1"
    goto :eof
)
for %%I in ("!OPT_PROJ_DIR!") do set "OPT_PROJ_DIR=%%~fI"
if "!OPT_PROJ_DIR:~-1!"=="\" (
    if not "!OPT_PROJ_DIR:~3,1!"=="" set "OPT_PROJ_DIR=!OPT_PROJ_DIR:~0,-1!"
)
goto :eof

:after_parse
pushd "%SCRIPT_ROOT%" >nul

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

if "%HELP_MODE%"=="1" (
    %PY_CMD% scripts\inspect_and_backup.py !OTHER_ARGS!
    set "EXIT_CODE=!errorlevel!"
    popd 2>nul
    endlocal & exit /b %EXIT_CODE%
)

echo ========================================================================
echo   Clean MIUI AI - One-Click Backup ^& Restore Verification
echo   Existing OS only (No root / No ROM flash). Checking ADB first...
echo ========================================================================
if "%HAS_PROJ_DIR%"=="1" (
    if "%MISSING_PROJ_VAL%"=="1" (
        %PY_CMD% scripts\inspect_and_backup.py !OTHER_ARGS! --project-dir
    ) else if "%BAD_PROJ_DIR%"=="1" (
        %PY_CMD% scripts\inspect_and_backup.py !OTHER_ARGS! --project-dir ""
    ) else (
        %PY_CMD% scripts\inspect_and_backup.py !OTHER_ARGS! --project-dir "!OPT_PROJ_DIR!"
    )
) else (
    %PY_CMD% scripts\inspect_and_backup.py !OTHER_ARGS!
)
set "EXIT_CODE=!errorlevel!"
if not "!EXIT_CODE!"=="0" (
    echo.
    echo [FAILED] Backup did not complete. Check docs\CONNECTION_GUIDE.md for USB or Wi-Fi ADB setup.
    goto maybe_pause
)

echo.
echo ========================================================================
echo   Verifying backup integrity & dry-run restore plan...
echo ========================================================================
if defined OPT_SERIAL (
    if defined OPT_PROJ_DIR (
        %PY_CMD% scripts\restore.py --serial "!OPT_SERIAL!" --project-dir "!OPT_PROJ_DIR!" --dry-run
    ) else (
        %PY_CMD% scripts\restore.py --serial "!OPT_SERIAL!" --dry-run
    )
) else (
    if defined OPT_PROJ_DIR (
        %PY_CMD% scripts\restore.py --project-dir "!OPT_PROJ_DIR!" --dry-run
    ) else (
        %PY_CMD% scripts\restore.py --dry-run
    )
)
set "EXIT_CODE=!errorlevel!"
if "!EXIT_CODE!"=="0" (
    echo.
    echo [OK] Backup created and dry-run restore verified!
    if defined OPT_PROJ_DIR (
        echo See "!OPT_PROJ_DIR!\backups\LATEST_BACKUP.txt" for your latest snapshot folder.
    ) else (
        echo See backups\LATEST_BACKUP.txt for your latest snapshot folder.
    )
) else (
    echo.
    echo [WARNING] Restore verification returned code !EXIT_CODE!. Review messages above before continuing.
)

:maybe_pause
popd >nul
echo %cmdcmdline% | find /i "%~f0" >nul
if %errorlevel%==0 (
    if /i not "%CLEAN_MIUI_NO_PAUSE%"=="1" pause
)
exit /b %EXIT_CODE%
