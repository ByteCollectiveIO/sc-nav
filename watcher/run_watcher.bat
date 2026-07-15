@echo off
rem SC Nav Watcher launcher — set your nav server address here (the bundle
rem downloaded from the web UI's Setup page arrives with this pre-filled):
set SERVER=http://YOUR-SERVER:8765
rem Your in-game handle (for attributing captured POIs/nodes). Once set, it is
rem remembered in watcher_config.json, so you can blank this out afterward.
set HANDLE=

title SC Nav Watcher
cd /d "%~dp0"

rem Find Python: try the py launcher first (the python.org installer provides
rem it even when the "Add python.exe to PATH" checkbox was missed), then
rem python (PATH / Microsoft Store installs). Running --version instead of
rem `where` filters out Windows' fake Store-stub python.exe, which exists even
rem on machines with no Python at all.
set PYTHON=
py --version >nul 2>nul && set PYTHON=py
if not defined PYTHON python --version >nul 2>nul && set PYTHON=python
if not defined PYTHON (
  echo Python was not found on this PC.
  echo Install Python 3.10+ from https://www.python.org/downloads/windows/
  echo then double-click this file again.
  pause
  exit /b 1
)

rem If no handle was set above, ask for one. Leave blank to reuse the handle
rem saved in watcher_config.json from a previous run. (Single-line IF on
rem purpose: the prompt text has parentheses, which would break a (...) block.)
if "%HANDLE%"=="" set /p HANDLE=Enter your in-game handle [blank = use saved]:

if "%HANDLE%"=="" (
  %PYTHON% sc_nav_watcher.py --server %SERVER%
) else (
  %PYTHON% sc_nav_watcher.py --server %SERVER% --handle "%HANDLE%"
)
pause
