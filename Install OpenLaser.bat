@echo off
rem OpenLaser one-step installer for Windows.
rem
rem Double-click this file. It:
rem   1. finds Python 3.10 or newer,
rem   2. sets up OpenLaser's private Python environment (.venv) and its dependencies,
rem   3. creates "OpenLaser" shortcuts on the Desktop and in the Start menu.
rem
rem Safe to run again at any time (after moving this folder, or to update).

setlocal
cd /d "%~dp0"
title OpenLaser installer
echo.
echo OpenLaser installer
echo Installing into: %CD%

echo.
echo ==^> Step 1 of 3: Looking for Python 3.10 or newer
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :no_python
%PY% --version

echo.
echo ==^> Step 2 of 3: Installing OpenLaser (this can take a minute)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 goto :install_failed
)
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
if errorlevel 1 goto :install_failed
".venv\Scripts\python.exe" -m pip install --quiet -e ".[desktop]"
if errorlevel 1 goto :install_failed

echo.
echo ==^> Step 3 of 3: Creating OpenLaser shortcuts
set "OPENLASER_DIR=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$dir = $env:OPENLASER_DIR; $shell = New-Object -ComObject WScript.Shell; foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { $link = $shell.CreateShortcut((Join-Path $folder 'OpenLaser.lnk')); $link.TargetPath = (Join-Path $dir '.venv\Scripts\openlaser-desktop.exe'); $link.Arguments = ''; $link.WorkingDirectory = $dir; $link.IconLocation = (Join-Path $dir 'assets\icons\openmopa.ico'); $link.Description = 'OpenLaser'; $link.Save() }"
if errorlevel 1 (
  echo Could not create the shortcuts. You can still start OpenLaser by double-clicking
  echo .venv\Scripts\openlaser-desktop.exe
)

echo.
echo Done! OpenLaser is installed.
echo.
echo   * Double-click "OpenLaser" on your Desktop (or find it in the Start menu).
echo     The OpenLaser desktop workspace opens.
echo   * Tip: right-click the shortcut and choose "Pin to taskbar".
echo   * To quit OpenLaser, close its window.
echo   * JCZ/LMC MOPA laser users: Windows needs the WinUSB driver for the
echo     laser first. See "Windows: connect a JCZ/LMC MOPA laser" in README.md.
echo   * If you move this folder somewhere else later, run this installer again.
echo.
pause
exit /b 0

:no_python
echo.
echo Python 3.10 or newer was not found on this computer.
echo.
echo Install it first (it is free and takes two minutes):
echo   1. Your browser will open python.org/downloads now.
echo   2. Download and run the Python installer.
echo   3. IMPORTANT: tick "Add python.exe to PATH" on the first screen.
echo   4. Double-click "Install OpenLaser.bat" again.
echo.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:install_failed
set "CODE=%errorlevel%"
echo.
echo The installation step failed (Python/pip exit code %CODE%).
echo The messages above say why. Fix that, then run this installer again.
echo If it keeps failing, please open an issue at
echo https://github.com/luizbueno3d/OpenLaser/issues with a screenshot of this window.
echo.
pause
exit /b 1
