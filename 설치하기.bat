@echo off
chcp 65001 > nul
echo.
echo  영상 자판기 설치를 시작합니다. (처음 한 번만, 2~5분)
echo.
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  [오류] 파이썬이 없거나 설치에 실패했어요. 사용설명서 1번을 확인하세요.
  pause
  exit /b
)
py -c "import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()"
echo.
echo  설치 완료! 이제 실행하기.bat 을 더블클릭하세요.
pause
