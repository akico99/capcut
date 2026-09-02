@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 캡컷 에이전트 서버를 시작합니다...
start "" http://127.0.0.1:8000

python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload

pause
