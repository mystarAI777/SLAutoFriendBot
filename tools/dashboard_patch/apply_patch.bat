@echo off
cd /d %~dp0
if not exist dashboard.py (
  echo dashboard.py が見つかりません。このバッチを dashboard.py と同じフォルダに置いてください。
  pause
  exit /b 1
)
where uv >nul 2>&1
if errorlevel 1 (
  echo uv not found. Install uv first.
  pause
  exit /b 1
)
uv run python apply_patch.py
pause
