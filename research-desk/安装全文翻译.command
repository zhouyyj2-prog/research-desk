#!/bin/bash
# 可选功能：安装“全文中英对照翻译”（开源项目 PDFMathTranslate-next 的 pdf2zh_next）。
# 只需双击一次。装在本文件夹的 translator/ 里，不影响电脑上其他程序；不想要了直接删掉 translator 文件夹即可。
cd "$(dirname "$0")" || exit 1
echo "== 安装全文翻译（大约需要 5–15 分钟，取决于网速）=="

PY=""
CANDS="python3.12 python3.13 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 python3"
for c in $CANDS; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)' >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  echo "全文翻译需要 Python 3.10 – 3.13，你的电脑上没找到。"
  echo "请到 https://www.python.org/downloads/ 下载安装 Python 3.12，装好后再双击本文件。"
  read -n 1 -s -r -p "按任意键关闭"; exit 1
fi
echo "使用 Python：$PY"

if [ ! -x translator/venv/bin/python ]; then
  mkdir -p translator && "$PY" -m venv translator/venv || { echo "创建环境失败"; read -n 1 -s -r -p "按任意键关闭"; exit 1; }
fi
VP=translator/venv/bin/python
"$VP" -m pip install -q --upgrade pip
echo "正在下载安装翻译程序……"
if ! "$VP" -m pip install --upgrade pdf2zh-next; then
  echo "官方源下载失败，改用清华镜像重试……"
  "$VP" -m pip install --upgrade pdf2zh-next -i https://pypi.tuna.tsinghua.edu.cn/simple || { echo; echo "安装没有成功。请截图这个窗口，到项目的 Issues 里反馈。"; read -n 1 -s -r -p "按任意键关闭"; exit 1; }
fi
if [ -x translator/venv/bin/pdf2zh_next ]; then
  echo; echo "安装完成！重新打开研究台，在阅读器里点“全文翻译”即可。"
  echo "提示：第一次翻译时还会自动下载版面识别模型，会多等几分钟。默认使用免费的翻译服务，不需要 API Key。"
else
  echo; echo "安装好像没有完成（没找到 pdf2zh_next）。请截图这个窗口反馈。"
fi
read -n 1 -s -r -p "按任意键关闭"
