#!/usr/bin/env bash
#
# 股票智能分析系统（DSA）- Hostinger VPS 一键部署脚本（Ubuntu 22.04+）
#
# 作用：把本项目部署成「域名 + HTTPS 访问」的常驻 Web 服务。
# 自动完成：装系统依赖（含 Node.js，用于构建前端）→ 建 Python 虚拟环境装依赖
#          → 构建前端静态资源 → 配 systemd 常驻服务 → 配 Nginx 反向代理
#          → 申请 Let's Encrypt 免费 HTTPS 证书。
#
# 与同服务器上其他项目共存：本脚本使用独立的 systemd 服务名（dsa）和独立的
# Nginx 配置文件，不会覆盖同服务器上其他项目（如用不同服务名的项目）已有的
# systemd/Nginx 配置；只要各项目使用不同端口、不同域名（或子域名）即可共存。
#
# 用法（在 VPS 上、已 clone 本仓库后，以 root 运行）：
#   sudo bash scripts/deploy_vps.sh 你的域名或子域名
#
# 例如：
#   sudo bash scripts/deploy_vps.sh dsa.example.com
#
set -euo pipefail

# ---------- 参数与路径 ----------
DOMAIN="${1:?用法: sudo bash scripts/deploy_vps.sh 你的域名或子域名}"

# 自动定位仓库根目录（本脚本在 scripts/ 下，根目录是它的上一级）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE=dsa

echo "==> 部署目录：$APP_DIR"
echo "==> 绑定域名：$DOMAIN"
echo "==> systemd 服务名：$SERVICE"

# ---------- 1. 系统依赖 ----------
echo "==> [1/9] 安装系统依赖（python、nginx、certbot 等）..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential \
    git curl nginx certbot python3-certbot-nginx

# Node.js 20+（构建 Web 前端需要；Ubuntu 22.04 自带的 nodejs 版本过旧）
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/^v//' | cut -d. -f1)" -lt 20 ]; then
    echo "==> 安装 Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

# ---------- 2. Python 虚拟环境 + 项目依赖 ----------
echo "==> [2/9] 创建虚拟环境并安装项目依赖..."
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ---------- 3. 构建 Web 前端静态资源 ----------
echo "==> [3/9] 构建 Web 前端..."
(cd "$APP_DIR/apps/dsa-web" && npm ci && npm run build)

# ---------- 4. 环境变量文件（含 AI Key、访问认证开关）----------
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "!! 已生成 $APP_DIR/.env，请先填写后再重跑本脚本："
    echo "   编辑命令： nano $APP_DIR/.env"
    echo "   至少填： STOCK_LIST=你的自选股代码"
    echo "           ANSPIRE_API_KEYS / GEMINI_API_KEY / DEEPSEEK_API_KEY 等 AI 模型 Key 中的一个（想要 AI 报告才需要）"
    echo "           ADMIN_AUTH_ENABLED=true（公网部署必填，见下方安全护栏说明）"
    exit 1
fi

# 安全护栏：公网部署必须开启 Web 登录认证，否则任何人都能打开系统设置页看到/改你的 API Key
if ! grep -qE '^ADMIN_AUTH_ENABLED=true' "$APP_DIR/.env"; then
    echo ""
    echo "!! 安全保护：.env 里必须设置 ADMIN_AUTH_ENABLED=true，否则任何人都能访问系统设置页并看到你的 API Key。"
    echo "   请编辑 $APP_DIR/.env 把该行改为 ADMIN_AUTH_ENABLED=true（首次访问网页时再设置登录密码）。"
    echo "   然后重跑本脚本。"
    exit 1
fi

# 已在第 3 步手动构建过前端，关闭启动时自动构建，避免每次重启服务都重新走一遍 npm 构建
if grep -qE '^WEBUI_AUTO_BUILD=' "$APP_DIR/.env"; then
    sed -i 's/^WEBUI_AUTO_BUILD=.*/WEBUI_AUTO_BUILD=false/' "$APP_DIR/.env"
else
    echo "WEBUI_AUTO_BUILD=false" >> "$APP_DIR/.env"
fi

# 读取监听端口（默认 8000），供下面 systemd/Nginx 配置复用
APP_PORT="$(grep -E '^WEBUI_PORT=' "$APP_DIR/.env" | tail -1 | cut -d '=' -f2)"
APP_PORT="${APP_PORT:-8000}"

# ---------- 5. 目录权限（服务以 www-data 运行，需要能写 data/logs/reports）----------
echo "==> [5/9] 设置目录权限..."
mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/reports"
chown -R www-data:www-data "$APP_DIR"

# ---------- 6. systemd 常驻服务（仅启动 Web/API，不含每日定时分析）----------
echo "==> [6/9] 配置 systemd 常驻服务..."
cat > /etc/systemd/system/${SERVICE}.service <<UNIT
[Unit]
Description=Daily Stock Analysis Web/API service (FastAPI)
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python main.py --serve-only --host 127.0.0.1 --port ${APP_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable ${SERVICE}
systemctl restart ${SERVICE}

# ---------- 7. Nginx 反向代理（把域名转发到本地端口）----------
echo "==> [7/9] 配置 Nginx 反向代理..."
cat > /etc/nginx/sites-available/${SERVICE} <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # Agent 问股页面依赖 WebSocket，以下两行必不可少
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/${SERVICE} /etc/nginx/sites-enabled/${SERVICE}
nginx -t
systemctl reload nginx

# ---------- 8. 防火墙（若启用了 ufw，放行 web 端口）----------
echo "==> [8/9] 配置防火墙（如有 ufw）..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow 'Nginx Full' || true
fi

# ---------- 9. HTTPS 证书（Let's Encrypt，免费、自动续期）----------
echo "==> [9/9] 申请 HTTPS 证书..."
if certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "admin@${DOMAIN}" --redirect; then
    echo "==> HTTPS 配置成功"
else
    echo ""
    echo "!! HTTPS 申请失败，通常是域名 DNS 还没解析到本机。"
    echo "   请确认 ${DOMAIN} 的 A 记录已指向本 VPS 的 IP 并生效后，再单独执行："
    echo "   sudo certbot --nginx -d ${DOMAIN}"
fi

echo ""
echo "========================================"
echo "✅ 部署完成！"
echo "   访问： https://${DOMAIN}"
echo "   首次打开网页需要设置登录密码（ADMIN_AUTH_ENABLED=true 已开启）"
echo ""
echo "   查看运行状态： systemctl status ${SERVICE}"
echo "   查看实时日志： journalctl -u ${SERVICE} -f"
echo ""
echo "   注意：本脚本只部署 Web/API 服务，不包含每日定时分析。"
echo "   如需服务器每日自动分析，可另行配置 --schedule 常驻或使用 GitHub Actions（见 docs/DEPLOY.md 方案四）。"
echo "========================================"
