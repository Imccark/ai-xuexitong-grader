@echo off
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 uv，请先安装 uv 后重新双击本文件。
  echo 安装说明：https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)

echo [1/2] 正在检查运行环境...
uv sync --extra orientation --no-dev
if errorlevel 1 (
  echo [错误] 运行环境安装失败，请检查网络后重试。
  pause
  exit /b 1
)

echo [2/2] 正在启动本地控制台...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
uv run python -m app.review_app --port 8765

pause
