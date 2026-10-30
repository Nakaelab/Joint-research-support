@echo off
cd /d "%~dp0"
echo ===================================================
echo  Fukui University Research Matching - GitHub Push
echo  Repo: https://github.com/Nakaelab/Joint-research-support.git
echo ===================================================
echo.
git config http.sslBackend schannel
git branch -M main
git remote set-url origin https://Nakaelab@github.com/Nakaelab/Joint-research-support.git
git add .
git commit -m "Update app" 2>nul
git push -u origin main
echo.
echo ===================================================
echo  Done! Press any key to close.
echo ===================================================
pause
