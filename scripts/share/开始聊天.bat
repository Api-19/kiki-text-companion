@echo off
chcp 65001 >nul
setlocal
set "RESULT=1"
for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"
if errorlevel 1 goto finish

"%REPO_ROOT%\.venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo 请先运行“开始设置”
    goto finish
)
set "SHARE_ROOT=%LOCALAPPDATA%\kiki-share"
set "COMPANION_PRIVATE_DATA_DIR=%SHARE_ROOT%\private"
set "COMPANION_RUNTIME_DATA_DIR=%SHARE_ROOT%\runtime"
set "COMPANION_TEST_RESULTS_DIR=%SHARE_ROOT%\test-results"
"%REPO_ROOT%\.venv\Scripts\python.exe" -m companion.runtime.text_chat --project-root "%REPO_ROOT%"
set "RESULT=%ERRORLEVEL%"

:finish
pause
exit /b %RESULT%
