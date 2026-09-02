@echo off
setlocal

cd /d "%~dp0"

if not exist logs mkdir logs

REM Force UTF-8 output for Python and Windows console
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%date% %time%] Starting daily ETL >> logs\scheduled-daily.log

.\.venv\Scripts\python.exe -m konsido_etl.cli run --mode TRUNCATE --log-file logs\konsido-etl-daily.log >> logs\scheduled-daily.log 2>&1

set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] Finished daily ETL with exit code %EXITCODE% >> logs\scheduled-daily.log

exit /b %EXITCODE%
