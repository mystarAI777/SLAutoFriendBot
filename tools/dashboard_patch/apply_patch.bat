@echo off
cd /d %~dp0
where uv >nul 2>&1
if errorlevel 1 (
  echo uv not found. Install uv first.
  pause
  exit /b 1
)
if exist dashboard.py uv run python apply_patch.py
if exist services.json uv run python apply_services.py
echo.
echo 完了。ダッシュボードを再起動してください。
pause
