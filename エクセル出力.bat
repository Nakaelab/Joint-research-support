@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo データベース(.db)からエクセル(.xlsx)へ出力しています...
python export_excel_db.py
echo.
echo 完了しました！何かキーを押すと閉じます。
pause > nul
