@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python -m voicekit --config config.example.json demo
) else (
  py -3 -m voicekit --config config.example.json demo
)
set "voicekit_exit=%errorlevel%"
echo.
echo 此演示仅生成提示音与模拟证据，不是台词配音。
pause
exit /b %voicekit_exit%
