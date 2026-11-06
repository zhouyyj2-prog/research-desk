#!/bin/bash
# 双击运行研究台（Mac）。关闭这个终端窗口即退出程序。
cd "$(dirname "$0")" || exit 1
HERE="$(pwd -P)"

PY=""
CANDS="python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 $HOME/miniconda3/bin/python3 $HOME/anaconda3/bin/python3 $HOME/opt/anaconda3/bin/python3 /opt/anaconda3/bin/python3 /opt/miniconda3/bin/python3 /usr/bin/python3"
ZPY=$(/bin/zsh -lic 'command -v python3' 2>/dev/null | tail -1)
[ -n "$ZPY" ] && CANDS="$ZPY $CANDS"
for c in $CANDS; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys,sqlite3; sys.exit(0 if sys.version_info>=(3,8) else 1)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "没有找到 Python 3.8 以上版本。"
  echo "请打开“终端”运行 xcode-select --install 安装苹果命令行工具（自带 Python 3），装好后再双击本文件。"
  read -n 1 -s -r -p "按任意键关闭"; exit 1
fi

# 找端口：8765 起。端口上已经是这一份研究台就直接打开；被别的程序占着就换下一个
PORT=${RD_PORT:-8765}
for i in $(seq 1 20); do
  R=$(curl -s -m 1 -X POST "http://127.0.0.1:$PORT/api/ping" 2>/dev/null)
  [ -z "$R" ] && break
  if printf '%s' "$R" | "$PY" -c 'import sys,json,os; d=json.load(sys.stdin); r=d.get("root"); sys.exit(0 if r and os.path.realpath(r)==os.path.realpath(sys.argv[1]) else 1)' "$HERE" 2>/dev/null; then
    open "http://127.0.0.1:$PORT"; exit 0
  fi
  PORT=$((PORT+1))
done

URL="http://127.0.0.1:$PORT"
echo "使用 Python：$PY"
echo "研究台地址：$URL"
(sleep 1.5; open "$URL") &
ulimit -n 10240 2>/dev/null  # 整篇 PDF 翻译会打开很多文件，macOS 默认 256 不够
export RD_PORT=$PORT
exec "$PY" app/server.py
