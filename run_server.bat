@echo off
chcp 65001 >nul
echo =========================================
echo 플라스크 서버 시작 스크립트 (포트: 9200)
echo =========================================
echo.

echo 1. 기존에 돌고 있는 서버가 있다면 종료합니다...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :9200') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul
echo.

echo =========================================
echo 🚀 서버가 시작됩니다! 브라우저에서 아래 링크를 클릭(또는 복사)해서 접속하세요.
echo ▶ 접속 주소 : http://127.0.0.1:9200
echo =========================================
echo.

echo 2. 가상 환경(.venv)을 활성화하고 서버를 켭니다...
call .venv\Scripts\activate.bat
python app.py

echo.
echo 서버가 종료되었습니다.
pause
