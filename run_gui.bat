@echo off
setlocal
cd /d "%~dp0"
title BMS2IIDX

rem ---- locate Python ----
set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python311\python.exe" set "PYEXE=%ProgramFiles%\Python311\python.exe"
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  echo.
  echo [ERROR] Python not found. Install Python 3.11+ or add it to PATH.
  echo.
  pause
  exit /b 1
)

rem ---- dependency check ----
%PYEXE% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] PySide6 missing for: %PYEXE%
  echo         Run once:  %PYEXE% -m pip install PySide6
  echo.
  pause
  exit /b 1
)

rem ---- prefer pythonw (no console window; in-app CLI pane carries the logs) ----
set "PYW="
set "CAND=%PYEXE:python.exe=pythonw.exe%"
if not "%CAND%"=="%PYEXE%" if exist "%CAND%" set "PYW=%CAND%"
if not defined PYW (
  where pythonw >nul 2>nul
  if not errorlevel 1 set "PYW=pythonw"
)

if defined PYW (
  start "" "%PYW%" -m gui.ui
  exit /b 0
)

echo No pythonw found - starting with console ...
%PYEXE% -m gui.ui
if errorlevel 1 (
  echo.
  echo [ERROR] exited with code %ERRORLEVEL%.
  echo.
  pause
)
exit /b 0
