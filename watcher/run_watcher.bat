@echo off
rem SC Nav Watcher launcher — set your nav server address here (the bundle
rem downloaded from the web UI's Setup page arrives with this pre-filled):
set SERVER=http://YOUR-SERVER:8765
rem Your in-game handle (for attributing captured POIs/nodes). Normally leave
rem this blank: the watcher reads the account you're signed in as straight from
rem Game.log, which can't be mistyped. Set it only to override that.
set HANDLE=
rem In-game overlay: L (light HUD), H (heavy beta, opens the web app in a pinned
rem browser window), or N (none) to answer without being prompted; blank to be
rem asked. Your answer is remembered in watcher_config.json, so the prompt takes
rem a blank afterward to mean "same as last time". (Y still means light, so an
rem older copy of this file keeps working.)
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

rem If no handle was set above, offer to take one. Blank is the normal answer —
rem the handle is read from Game.log at sign-in. (Single-line IF on purpose: the
rem prompt text has parentheses, which would break a (...) block.)
if "%HANDLE%"=="" set /p HANDLE=In-game handle [blank = read it from Game.log]:

rem Same deal for the overlay. Two flavours: LIGHT is the small always-on-top
rem HUD (target/distance/ETA); HEAVY is the beta that opens the whole web app in
rem a browser window pinned over the game. Off until you say otherwise.
rem (Single-line IF again — the prompt text has parentheses, which would break a
rem (...) block.)
if "%OVERLAY%"=="" echo.
if "%OVERLAY%"=="" echo   Overlay:  L = light HUD (target/distance/ETA)
if "%OVERLAY%"=="" echo             H = heavy BETA (full web app, pinned browser window)
if "%OVERLAY%"=="" echo             N = none
if "%OVERLAY%"=="" set /p OVERLAY=Choose [L/H/N, blank = use saved]:

rem The overlay needs tkinter, which is part of Python itself — there is nothing
rem to pip-install. It comes from the "tcl/tk and IDLE" option in the python.org
rem installer (ticked by default), so it's only missing if someone unticked it.
rem Say so plainly here rather than letting the watcher log one line and move on;
rem the person who just asked for the overlay is the one who needs to read it.
rem (goto/errorlevel rather than a nested `||` inside an IF block — batch parses
rem that inconsistently, and this file has to work first try on someone else's PC.)
rem Only the LIGHT overlay needs tkinter; heavy mode drives a browser instead.
if /i "%OVERLAY%"=="L" goto tk_check
if /i "%OVERLAY%"=="Y" goto tk_check
goto tk_ok
:tk_check
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
if /i "%OVERLAY%"=="L" set ARGS=%ARGS% --overlay-mode light
if /i "%OVERLAY%"=="Y" set ARGS=%ARGS% --overlay-mode light
if /i "%OVERLAY%"=="H" set ARGS=%ARGS% --overlay-mode heavy
if /i "%OVERLAY%"=="N" set ARGS=%ARGS% --overlay-mode off

%PYTHON% sc_nav_watcher.py %ARGS%
pause
