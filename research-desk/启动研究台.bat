@echo off
chcp 65001 >nul
REM 双击运行研究台（Windows）。关闭这个黑色窗口即退出程序。
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo 没有找到 Python。请先到 https://www.python.org/downloads/ 下载安装，安装时勾选 "Add python.exe to PATH"，装好后再双击本文件。
  pause
  exit /b 1
)
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8765"
%PY% app\server.py
pause
