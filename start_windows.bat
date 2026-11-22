@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python не найден. Установите Python 3.11+ с python.org, отметив "Add python.exe to PATH", и запустите файл снова. & pause & exit /b 1)
if not exist .venv (
  echo Первый запуск: устанавливаю зависимости...
  python -m venv .venv || (pause & exit /b 1)
  findstr /v /i "gunicorn" requirements.txt > req_win.txt
  .venv\Scripts\python -m pip install -r req_win.txt || (echo Ошибка установки. Проверьте интернет. & pause & exit /b 1)
)
set APP_ENV=development
set DATABASE=%~dp0data\kadry.db
set UPLOAD_DIR=%~dp0data\uploads
set BACKUP_DIR=%~dp0data\backups
if not exist data\uploads mkdir data\uploads
if not exist data\backups mkdir data\backups
if not exist data\secret.txt .venv\Scripts\python -c "import secrets;open('data/secret.txt','w').write(secrets.token_hex(32))"
set /p SECRET_KEY=<data\secret.txt
if not exist data\kadry.db (
  set ADMIN_LOGIN=admin
  set ADMIN_PASSWORD=Kadry-Demo-2026!
  .venv\Scripts\python -m flask --app app init-db
  .venv\Scripts\python -m flask --app app bootstrap-admin
  echo Логин: admin   Пароль: Kadry-Demo-2026!  ^(смените после входа^)
)
start "" http://127.0.0.1:8000
.venv\Scripts\python -m flask --app app run --host 127.0.0.1 --port 8000
pause
