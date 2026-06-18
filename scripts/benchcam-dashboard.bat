@echo off
REM ---------------------------------------------------------------------------
REM BenchCam dashboard launcher (Windows).
REM Double-click this (or a desktop shortcut to it) to open the dashboard in
REM your browser. It activates the project's .venv and runs `benchcam dashboard`.
REM Keep the window open while you work; close it to stop the dashboard.
REM ---------------------------------------------------------------------------
setlocal

REM Repo root is the parent of this script's "scripts\" folder.
pushd "%~dp0.."

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo Could not find .venv in "%CD%".
    echo Create it first:  py -3 -m venv .venv  ^&^&  .venv\Scripts\activate  ^&^&  pip install -e .
    echo.
    pause
    popd
    exit /b 1
)

benchcam dashboard

popd
endlocal
