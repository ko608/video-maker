@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo  최신 버전으로 업데이트합니다...
git pull
py -m pip install -r requirements.txt
echo  완료!
pause
