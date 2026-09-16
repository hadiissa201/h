@echo off
REM Windows Task Scheduler entry point for the trading loop.
REM
REM Task Scheduler reports only a process exit code, so a task that fails to
REM launch looks identical to one that ran fine -- exit 0, no output, nothing in
REM the application log. This wrapper removes that blind spot: it resolves paths
REM relative to itself (%~dp0 is this file's folder, so .. is the repo root),
REM which makes it independent of the "Start in" setting, and appends both
REM stdout and stderr to logs\task-out.log so a launch failure is visible.
REM
REM Point the scheduled task at this file, not at python.exe.

cd /d "%~dp0.."
if not exist "logs" mkdir "logs"

echo. >> "logs\task-out.log"
echo ===== tick started %DATE% %TIME% ===== >> "logs\task-out.log"

".\python-service\.venv\Scripts\python.exe" ".\scripts\tick.py" >> "logs\task-out.log" 2>&1
set RC=%ERRORLEVEL%

echo ===== tick finished rc=%RC% ===== >> "logs\task-out.log"
exit /b %RC%
