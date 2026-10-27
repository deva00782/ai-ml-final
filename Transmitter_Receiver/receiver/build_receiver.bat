@echo off
setlocal
title SynaptiMesh BCI Receiver - PyInstaller Build Utility

echo ================================================================
echo   SynaptiMesh BCI Receiver - Standalone Windows Builder
echo ================================================================
echo Target: Windows 10 / 11 64-bit
echo Scope : PC2 Receiver Subsystem ONLY (Transmitter untouched)
echo.

REM Determine Python interpreter (prefer project venv if present)
set "PYTHON_EXE=python"
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
    echo [*] Using local virtual environment Python: %~dp0venv\Scripts\python.exe
) else (
    echo [*] Using system Python
)

REM Verify Python is functional
"%PYTHON_EXE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH or venv!
    echo         Please install Python 3.10+ 64-bit or activate your venv.
    pause
    exit /b 1
)

REM Check for CLI argument (e.g., build_receiver.bat onedir / onefile / all)
set "CHOICE=%~1"
if not "%CHOICE%"=="" goto PROCESS_CHOICE

:MENU
echo.
echo Please select a build target for Receiver.exe:
echo   [1] Build --onefile (Single Receiver.exe in dist\ - Recommended standalone)
echo   [2] Build --onedir  (Directory bundle in dist\Receiver\ - Fast startup)
echo   [3] Build Both (--onefile and --onedir)
echo   [4] Exit
echo.
set /p USER_SELECTION="Enter choice [1-4] (default=1): "
if "%USER_SELECTION%"=="" set "USER_SELECTION=1"
if "%USER_SELECTION%"=="1" set "CHOICE=onefile"
if "%USER_SELECTION%"=="2" set "CHOICE=onedir"
if "%USER_SELECTION%"=="3" set "CHOICE=all"
if "%USER_SELECTION%"=="4" goto END_SCRIPT

:PROCESS_CHOICE
if /i "%CHOICE%"=="onefile" goto BUILD_ONEFILE
if /i "%CHOICE%"=="onedir" goto BUILD_ONEDIR
if /i "%CHOICE%"=="all" goto BUILD_ALL
echo [ERROR] Invalid selection "%CHOICE%".
goto MENU

:BUILD_ONEFILE
echo.
echo ================================================================
echo   Building TARGET: Standalone Single Binary (dist\Receiver.exe)
echo ================================================================
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean Receiver.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller failed to build Receiver.exe (--onefile)!
    pause
    exit /b 1
)

REM Ensure external editable config.json is deployed alongside Receiver.exe
if not exist "dist\agents" mkdir "dist\agents"
copy /y "agents\config.json" "dist\agents\config.json" >nul
copy /y "agents\config.json" "dist\config.json" >nul

echo.
echo [SUCCESS] Standalone build completed!
echo           Binary: dist\Receiver.exe
echo           Config: dist\config.json (External / Editable)
echo.
if /i "%CHOICE%"=="all" goto BUILD_ONEDIR
goto FINISH

:BUILD_ONEDIR
echo.
echo ================================================================
echo   Building TARGET: Directory Bundle (dist\Receiver\Receiver.exe)
echo ================================================================
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean Receiver_onedir.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller failed to build Receiver.exe (--onedir)!
    pause
    exit /b 1
)

REM Ensure external editable config.json is deployed into dist\Receiver\ folder
if not exist "dist\Receiver\agents" mkdir "dist\Receiver\agents"
copy /y "agents\config.json" "dist\Receiver\agents\config.json" >nul
copy /y "agents\config.json" "dist\Receiver\config.json" >nul

echo.
echo [SUCCESS] Directory bundle build completed!
echo           Folder: dist\Receiver\
echo           Binary: dist\Receiver\Receiver.exe
echo           Config: dist\Receiver\config.json (External / Editable)
echo.
goto FINISH

:BUILD_ALL
goto BUILD_ONEFILE

:FINISH
echo ================================================================
echo   RECEIVER BUILD COMPLETED SUCCESSFULLY!
echo ================================================================
echo.
echo  To run/test on this PC:
echo    Standalone: dist\Receiver.exe --list-ports
echo    Onedir    : dist\Receiver\Receiver.exe --list-ports
echo.
echo  To deploy to PC2 (without Python):
echo    Copy "dist\Receiver.exe" + "dist\config.json"
echo    OR copy the entire "dist\Receiver" folder
echo ================================================================
if "%~1"=="" pause
:END_SCRIPT
