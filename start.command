#!/bin/bash
# macOS 双击启动 / Linux 运行 ./start.command
cd "$(dirname "$0")"

# 固德威自动同步（可选）：去掉下面两行开头的 # 并填写 SEMS 账号
# export GOODWE_ACCOUNT=你的账号
# export GOODWE_PASSWORD=你的密码

if ! python3 -c "import sys; sys.exit(sys.version_info < (3, 8))" 2>/dev/null; then
  echo "没有找到 Python 3（需要 3.8 或更高版本）。"
  echo "请从 https://www.python.org/downloads/ 下载安装后，再双击本文件。"
  open "https://www.python.org/downloads/" 2>/dev/null
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi
python3 run.py
read -n 1 -s -r -p "平台已停止，按任意键关闭…"
