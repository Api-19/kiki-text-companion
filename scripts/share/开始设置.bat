@echo off
chcp 65001 >nul
setlocal
set "RESULT=1"
for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"
if errorlevel 1 goto finish

"%REPO_ROOT%\.venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 goto install
if exist "%REPO_ROOT%\.venv\" (
    echo 项目里的 .venv 环境不可用（可能是从别的电脑拷贝过来的）。请删除项目文件夹里的 .venv 文件夹，再重新运行“开始设置”。聊天数据和设置不在 .venv 里，不会丢失。
    goto finish
)

py -3.12 -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py -3.12"
    goto python_found
)
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python"
    goto python_found
)
echo 没有找到 Python 3.12，请先按说明安装
goto finish

:python_found
%PYTHON% -m venv "%REPO_ROOT%\.venv"
if errorlevel 1 goto finish
:install
"%REPO_ROOT%\.venv\Scripts\python.exe" -m pip install -q -e ".[share]"
if errorlevel 1 (
    echo 安装依赖失败，请检查网络后重试
    goto finish
)
"%REPO_ROOT%\.venv\Scripts\python.exe" "%REPO_ROOT%\scripts\setup_share.py" %*
set "RESULT=%ERRORLEVEL%"

:finish
pause
exit /b %RESULT%
