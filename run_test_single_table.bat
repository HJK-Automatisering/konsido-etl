@echo off
setlocal

cd /d "%~dp0"

if not exist logs mkdir logs

REM Force UTF-8 output for Python and Windows console
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Tabel kan angives som argument: run_test_single_table.bat fact_spend
REM Uden argument bruges en lille dimensionstabel, saa scriptet er billigt at koere.
set TABLE=%~1
if "%TABLE%"=="" set TABLE=dim_date

echo [%date% %time%] Starting single-table ETL test (%TABLE%) >> logs\scheduled-run-single-table.log

.\.venv\Scripts\python.exe -m konsido_etl.cli run --mode TRUNCATE --table %TABLE% --log-file logs\run-single-table.log >> logs\scheduled-run-single-table.log 2>&1

set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] Finished single-table ETL test (%TABLE%) with exit code %EXITCODE% >> logs\scheduled-run-single-table.log

exit /b %EXITCODE%
