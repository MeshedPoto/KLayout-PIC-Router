@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

if exist "%SCRIPT_DIR%\pic_router\grain.xml" (
    set "SRC_DIR=%SCRIPT_DIR%\pic_router"
) else if exist "%SCRIPT_DIR%\grain.xml" (
    set "SRC_DIR=%SCRIPT_DIR%"
) else if exist "%SCRIPT_DIR%\..\grain.xml" (
    for %%I in ("%SCRIPT_DIR%\..") do set "SRC_DIR=%%~fI"
) else (
    echo [ERROR] Could not find grain.xml relative to this script.
    echo Please extract the full release archive or run from the repository root/scripts folder.
    pause
    exit /b 1
)

if defined KLAYOUT_HOME (
    set "KLAYOUT_USER_DIR=%KLAYOUT_HOME%"
) else (
    set "KLAYOUT_USER_DIR=%USERPROFILE%\KLayout"
)
set "SALT_DIR=%KLAYOUT_USER_DIR%\salt"
set "TARGET_DIR=%SALT_DIR%\pic_router"

if /I "%~1"=="--uninstall" goto :uninstall
if /I "%~1"=="-u" goto :uninstall

echo ============================================================
echo  Installing KLayout-PIC Router (Salt package: pic_router)
echo ============================================================
echo Source : %SRC_DIR%
echo Target : %TARGET_DIR%
echo.

if not exist "%SALT_DIR%" mkdir "%SALT_DIR%"
if exist "%TARGET_DIR%" (
    echo Removing previous installation at "%TARGET_DIR%" ...
    rmdir /S /Q "%TARGET_DIR%"
    if exist "%TARGET_DIR%" (
        echo [ERROR] Failed to remove old folder. Please close KLayout first and try again.
        pause
        exit /b 1
    )
)

mkdir "%TARGET_DIR%\pymacros"
mkdir "%TARGET_DIR%\python\pic_router"

copy /Y "%SRC_DIR%\grain.xml" "%TARGET_DIR%\grain.xml" >nul
if errorlevel 1 goto :copy_fail

copy /Y "%SRC_DIR%\pymacros\pic_router_init.lym" "%TARGET_DIR%\pymacros\pic_router_init.lym" >nul
if errorlevel 1 goto :copy_fail

copy /Y "%SRC_DIR%\python\pic_router\*.py" "%TARGET_DIR%\python\pic_router\" >nul
if errorlevel 1 goto :copy_fail

echo [OK] KLayout-PIC Router installed successfully!
echo.
echo Next steps:
echo   1. Restart KLayout (supported: KLayout 0.28 / 0.29 / 0.30+).
echo   2. Enable Editing Mode if needed:
echo      File -^> Setup -^> Application -^> Editing Mode (then restart KLayout).
echo   3. Look for the "PIC Tools" menu and right-side "PIC Snap ^& Router" panel (Ctrl+Shift+T).
echo   4. See README.md / README_zh.md for the full user guide and shortcuts.
echo.
pause
exit /b 0

:uninstall
echo Uninstalling pic_router from "%TARGET_DIR%" ...
if exist "%TARGET_DIR%" (
    rmdir /S /Q "%TARGET_DIR%"
    if exist "%TARGET_DIR%" (
        echo [ERROR] Could not remove "%TARGET_DIR%". Close KLayout and retry.
        pause
        exit /b 1
    )
    echo [OK] pic_router uninstalled. Restart KLayout to complete removal.
) else (
    echo pic_router is not currently installed at "%TARGET_DIR%".
)
pause
exit /b 0

:copy_fail
echo [ERROR] File copy failed. Check folder permissions and ensure KLayout is closed.
pause
exit /b 1
