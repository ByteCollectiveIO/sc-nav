@echo off
rem SC Nav Watcher launcher — set your nav server address here (the bundle
rem downloaded from the web UI's Setup page arrives with this pre-filled):
set SERVER=http://YOUR-SERVER:8765
rem Your in-game handle (for attributing captured POIs/nodes). Once set, it is
rem remembered in watcher_config.json, so you can blank this out afterward.
set HANDLE=

title SC Nav Watcher
cd /d "%~dp0"

rem If no handle was set above, ask for one. Leave blank to reuse the handle
rem saved in watcher_config.json from a previous run. (Single-line IF on
rem purpose: the prompt text has parentheses, which would break a (...) block.)
if "%HANDLE%"=="" set /p HANDLE=Enter your in-game handle [blank = use saved]:

if "%HANDLE%"=="" (
  python sc_nav_watcher.py --server %SERVER%
) else (
  python sc_nav_watcher.py --server %SERVER% --handle "%HANDLE%"
)
pause
