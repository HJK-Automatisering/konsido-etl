@echo off
setlocal

cd /d "%~dp0"

if not exist logs mkdir logs

REM Force UTF-8 output for Python and Windows console
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [%date% %time%] Starting schema refresh >> logs\scheduled-schema-refresh.log

.\.venv\Scripts\python.exe -m konsido_etl.cli run ^
  --mode DROP_CREATE ^
  --table dim_chart_of_accounts ^
  --table fact_spend ^
  --log-file logs\konsido-etl-schema-refresh.log >> logs\scheduled-schema-refresh.log 2>&1

set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] Finished schema refresh with exit code %EXITCODE% >> logs\scheduled-schema-refresh.log

exit /b %EXITCODE%
