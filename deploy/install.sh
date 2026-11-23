#!/bin/bash
# 在云服务器（Ubuntu / Debian / CentOS 等使用 systemd 的 Linux）上一键安装为系统服务。
# 用法（在代码目录中，以 root 运行）：  sudo bash deploy/install.sh
# 重复运行即为升级：会更新代码并重启，数据和配置保留。
set -euo pipefail

APP_DIR=/opt/solar-ems
DATA_DIR=/var/lib/solar-ems
ENV_FILE=/etc/solar-ems.env
SERVICE=/etc/systemd/system/solar-ems.service
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "请用 root 运行：sudo bash $0"; exit 1; fi

# 找一个 >= 3.9 的 Python（阿里云 Alibaba Cloud Linux / CentOS 自带的 python3 是 3.6，太旧）
find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys, sqlite3; sys.exit(sys.version_info < (3, 9))" 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "正在安装 Python 3（约 1 分钟）…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3 >/dev/null
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3.11 >/dev/null 2>&1 || dnf install -y -q python39 >/dev/null
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3.11 >/dev/null 2>&1 || yum install -y -q python39 >/dev/null
  fi
  PY="$(find_python || true)"
fi
if [ -z "$PY" ]; then
  echo "❌ 没能自动安装 Python 3.9+。建议在阿里云控制台把系统重装为 Ubuntu 22.04 后再试。"
  exit 1
fi
echo "使用 $("$PY" --version)（$PY）"

id solar >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin solar
mkdir -p "$APP_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/solar"
cp -r "$SRC_DIR/solar" "$SRC_DIR/run.py" "$APP_DIR/"
chown -R solar: "$DATA_DIR"

# 自检：确认这台服务器的 Python / SQLite 能正常运行平台
if ! (cd "$APP_DIR" && "$PY" - <<'CHECK'
from solar import db
c = db.connect(":memory:")
s = db.create_station(c, {"name": "t", "capacity_kwp": 1, "price": 1})
db.upsert_record(c, s["id"], "2024-01-01", 1)
db.upsert_record(c, s["id"], "2024-01-01", 2)
assert db.summary(c, None)["total"]["kwh"] == 2
assert db.monthly(c, None, 2024)["items"][0]["kwh"] == 2
CHECK
); then
  echo "❌ 自检失败：此服务器的 Python/SQLite 版本不兼容。请把上面的报错发给开发者，或将系统重装为 Ubuntu 22.04。"
  exit 1
fi
chmod 700 "$DATA_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "首次安装，请设置平台登录密码（至少 8 位）："
  while true; do
    read -r -s -p "密码: " PW1; echo
    read -r -s -p "再输一次: " PW2; echo
    if [ "$PW1" != "$PW2" ]; then echo "两次输入不一致"; continue; fi
    if [ "${#PW1}" -lt 8 ]; then echo "密码太短"; continue; fi
    break
  done
  umask 077
  cat > "$ENV_FILE" <<CONF
# 光伏能源管理平台配置。修改后执行：systemctl restart solar-ems
SOLAR_PASSWORD=$PW1
SOLAR_PORT=8000
SOLAR_TZ=Asia/Shanghai
SOLAR_DB=$DATA_DIR/solar.db

# 固德威 SEMS 自动同步（可选）：去掉 # 并填写
#GOODWE_ACCOUNT=
#GOODWE_PASSWORD=
#GOODWE_SYNC_MINUTES=15
# 区域：cn=中国区(cn-semsplus.goodwe.com)  eu / hk / au / us
#GOODWE_REGION=cn

# 如果前面加了 Nginx/Caddy 做 HTTPS 反向代理，改为 1
#SOLAR_TRUST_PROXY=1
#SOLAR_SECURE_COOKIE=1
CONF
  chmod 600 "$ENV_FILE"
fi

# 固德威连接诊断命令：sudo solar-goodwe-check [电站ID]
cat > /usr/local/bin/solar-goodwe-check <<WRAP
#!/bin/sh
cd $APP_DIR && exec $PY -m solar.goodwe_check "\$@"
WRAP
chmod 755 /usr/local/bin/solar-goodwe-check

cat > "$SERVICE" <<UNIT
[Unit]
Description=Solar PV energy management platform
After=network-online.target
Wants=network-online.target

[Service]
User=solar
EnvironmentFile=$ENV_FILE
Environment=SOLAR_HOST=0.0.0.0
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/run.py --no-browser
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable solar-ems >/dev/null 2>&1
systemctl restart solar-ems
sleep 2
PORT=$(grep -E '^SOLAR_PORT=' "$ENV_FILE" | cut -d= -f2); PORT=${PORT:-8000}
if systemctl is-active --quiet solar-ems; then
  IP="<服务器公网IP>"
  echo
  echo "✅ 安装完成，平台已启动并设为开机自启。"
  echo "   访问地址: http://${IP}:${PORT}"
  echo "   记得在云服务器控制台的「防火墙/安全组」中放行 TCP ${PORT} 端口。"
  echo "   配置文件: $ENV_FILE   数据: $DATA_DIR/solar.db"
  echo "   查看日志: journalctl -u solar-ems -f"
else
  echo "❌ 启动失败，查看原因：journalctl -u solar-ems -n 50"
  exit 1
fi
