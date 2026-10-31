@echo off
rem Drag one or more slow motion videos of the latency test onto this file to analyse them.
rem Asks for the stream resolution and frame rate once, and where Moonlight's latency test CSVs are, writes the
rem results to a latency-results folder next to each video and opens each HTML report. Set LATENCY_STREAM,
rem LATENCY_STREAM_FPS and LATENCY_MARKER_LOG to change the defaults.
setlocal EnableDelayedExpansion

set "SCRIPT=%~dp0analyze_video.py"
if "%~1"=="" (
  echo Drag one or more videos onto this file to analyse them.
  echo See README.md next to it for how to record them.
  pause
  exit /b 1
)
where python >nul 2>nul || (
  echo Python was not found on PATH. Install Python 3 and run: python -m pip install numpy opencv-python-headless
  pause
  exit /b 1
)

if not defined LATENCY_STREAM set "LATENCY_STREAM=3840x2160"
if not defined LATENCY_STREAM_FPS set "LATENCY_STREAM_FPS=120"
set "STREAM="
set "STREAM_FPS="
set /p "STREAM=Stream resolution [%LATENCY_STREAM%]: "
if not defined STREAM set "STREAM=%LATENCY_STREAM%"
set /p "STREAM_FPS=Stream frame rate, 0 if unknown [%LATENCY_STREAM_FPS%]: "
if not defined STREAM_FPS set "STREAM_FPS=%LATENCY_STREAM_FPS%"
set "FPS_ARG="
if not "%STREAM_FPS%"=="0" set "FPS_ARG=--stream-fps %STREAM_FPS%"
rem Moonlight writes a CSV for each latency test to %TEMP% on Windows. With a folder, the analyzer picks the CSV
rem that matches each video.
if not defined LATENCY_MARKER_LOG set "LATENCY_MARKER_LOG=%TEMP%"
set "MARKER_LOG="
set "LOG_ARG="
set /p "MARKER_LOG=Moonlight latency test CSV, or a folder of them, - to skip [%LATENCY_MARKER_LOG%]: "
if not defined MARKER_LOG set "MARKER_LOG=%LATENCY_MARKER_LOG%"
set "MARKER_LOG=!MARKER_LOG:"=!"
if not "!MARKER_LOG!"=="-" set LOG_ARG=--marker-log "!MARKER_LOG!"

set FAILED=0
for %%V in (%*) do (
  echo.
  echo ===== %%~nxV =====
  set "OUT=%%~dpVlatency-results"
  python "%SCRIPT%" "%%~fV" --stream %STREAM% !FPS_ARG! !LOG_ARG! --report --out "!OUT!"
  if errorlevel 1 (
    set FAILED=1
  ) else (
    start "" "!OUT!\%%~nV.report.html"
  )
)

echo.
if "%FAILED%"=="1" echo Some videos could not be analysed; see the messages above.
pause
