@echo off
chcp 65001 >nul
REM 可选功能：安装“全文中英对照翻译”（PDFMathTranslate-next）。只需双击一次，装在本文件夹的 translator 里。
cd /d "%~dp0"
echo == 安装全文翻译（大约需要 5-15 分钟）==
set "PY="
for %%v in (3.12 3.13 3.11 3.10) do (
  if not defined PY ( py -%%v -c "import sys" >nul 2>nul && set "PY=py -%%v" )
)
if not defined PY (
  echo 全文翻译需要 Python 3.10 - 3.13。请到 https://www.python.org/downloads/ 安装 Python 3.12（勾选 Add python.exe to PATH），再双击本文件。
  pause
  exit /b 1
)
if not exist translator\venv\Scripts\python.exe ( %PY% -m venv translator\venv )
set "VP=translator\venv\Scripts\python.exe"
%VP% -m pip install -q --upgrade pip
echo 正在下载安装翻译程序……
%VP% -m pip install --upgrade pdf2zh-next || %VP% -m pip install --upgrade pdf2zh-next -i https://pypi.tuna.tsinghua.edu.cn/simple
if exist translator\venv\Scripts\pdf2zh_next.exe (
  echo 安装完成！重新打开研究台，在阅读器里点“全文翻译”即可。第一次翻译会自动下载模型，多等几分钟。
) else (
  echo 安装没有成功，请截图这个窗口到项目的 Issues 反馈。
)
pause
