@echo off
chcp 65001 >nul
echo =========================================
echo 플라스크 서버 시작 스크립트 (포트: 9200)
echo =========================================
echo.

echo =========================================
echo 🚀 서버가 시작됩니다! 브라우저에서 아래 링크를 클릭(또는 복사)해서 접속하세요.
echo ▶ 접속 주소 : http://127.0.0.1:9200
echo =========================================
echo.

echo 사용할 Python을 확인합니다...
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE (
  echo Python 3.11 실행 파일을 찾을 수 없습니다.
  echo .venv를 만들거나 Python 3.11을 설치한 뒤 다시 실행해주세요.
  exit /b 1
)

set AI_QUEUE_AUTOSTART=false
"%PYTHON_EXE%" -m flask --app app db upgrade
if errorlevel 1 (
  echo 데이터베이스 마이그레이션에 실패하여 서버를 시작하지 않습니다.
  exit /b 1
)
set AI_QUEUE_AUTOSTART=true
"%PYTHON_EXE%" app.py

echo.
echo 서버가 종료되었습니다.
pause
