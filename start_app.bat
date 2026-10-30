@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist "fukui_matching.db" (
    echo データベースを初期化しています...
    python build_db.py
)
start "" "http://127.0.0.1:8000"
echo 福井大学 研究連携サポートサーバーを起動しています (http://127.0.0.1:8000)...
python server.py
pause
