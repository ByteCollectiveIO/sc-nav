@echo off
rem SC Nav Watcher launcher — set your nav server address here (the bundle
rem downloaded from the web UI's Setup page arrives with this pre-filled):
set SERVER=http://YOUR-SERVER:8765
rem Your in-game handle (for attributing captured POIs/nodes). Once set, it is
rem remembered in watcher_config.json, so you can blank this out afterward.
set HANDLE=
rem In-game overlay: Y or N to answer without being prompted, blank to be asked
rem (your answer is remembered in watcher_config.json, so the prompt takes a
rem blank afterward to mean "same as last time").
set OVERLAY=

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

rem Same deal for the overlay: a small always-on-top window over the game with
rem your target, distance and ETA. Off until you say otherwise. (Single-line IF
rem again — the prompt text has parentheses, which would break a (...) block.)
if "%OVERLAY%"=="" set /p OVERLAY=Show the in-game overlay (target/distance)? [Y/N, blank = use saved]:

rem The overlay needs tkinter, which is part of Python itself — there is nothing
rem to pip-install. It comes from the "tcl/tk and IDLE" option in the python.org
rem installer (ticked by default), so it's only missing if someone unticked it.
rem Say so plainly here rather than letting the watcher log one line and move on;
rem the person who just asked for the overlay is the one who needs to read it.
rem (goto/errorlevel rather than a nested `||` inside an IF block — batch parses
rem that inconsistently, and this file has to work first try on someone else's PC.)
if /i not "%OVERLAY%"=="Y" goto tk_ok
%PYTHON% -c "import tkinter" >nul 2>nul
if not errorlevel 1 goto tk_ok
echo.
echo   The overlay needs Python's "tcl/tk and IDLE" component, which this Python
echo   was installed without. To add it:
echo.
echo     Settings ^> Apps ^> Python ^> Modify ^> tick "tcl/tk and IDLE" ^> Install
echo.
echo   ...or reinstall from https://www.python.org/downloads/windows/ leaving that
echo   box ticked. The watcher itself runs fine meanwhile - you just won't see the
echo   overlay.
echo.
pause
:tk_ok

rem Build the argument list up instead of branching: with two tri-state answers
rem a nested-IF version would need four copies of the command line.
set ARGS=--server %SERVER%
if not "%HANDLE%"=="" set ARGS=%ARGS% --handle "%HANDLE%"
if /i "%OVERLAY%"=="Y" set ARGS=%ARGS% --overlay
if /i "%OVERLAY%"=="N" set ARGS=%ARGS% --no-overlay

%PYTHON% sc_nav_watcher.py %ARGS%
pause
