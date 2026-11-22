#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="feishu-agy-bot"
INSTALL_DIR="/opt/${SERVICE_NAME}"
RUN_USER="${USER}"
USER_HOME="${HOME}"

# 1. 检查并安装 uv
if ! command -v uv >/dev/null 2>&1; then
    echo ">>> 未检测到 uv，正在自动安装..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${USER_HOME}/.local/bin:${USER_HOME}/.cargo/bin:${PATH}"
fi
UV_BIN="$(command -v uv)"
echo ">>> 使用 uv: ${UV_BIN}"

# 2. 检查 agy 路径（确保 systemd 能找到 agy）
AGY_BIN="$(command -v agy || echo "${USER_HOME}/.local/bin/agy")"
if [ ! -x "${AGY_BIN}" ]; then
    echo "[警告] 未在 PATH 中找到 agy，请确认已安装 Antigravity CLI。"
fi
AGY_DIR="$(dirname "${AGY_BIN}")"
UV_DIR="$(dirname "${UV_BIN}")"

# 3. 复制脚本到部署目录
echo ">>> 部署文件到 ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"
sudo cp ./feishu_agy_bot.py "${INSTALL_DIR}/feishu_agy_bot.py"
sudo chmod +x "${INSTALL_DIR}/feishu_agy_bot.py"

# 4. 生成 .env 配置文件（若已存在则不覆盖）
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    sudo tee "${INSTALL_DIR}/.env" >/dev/null <<EOF
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_ALLOWED_OPEN_ID=
AGY_WORK_DIR=${USER_HOME}
IMG_SAVE_PATH=/tmp/latest.png
EOF
    sudo chmod 600 "${INSTALL_DIR}/.env"
    echo ">>> 已生成配置文件: ${INSTALL_DIR}/.env"
fi
sudo chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"

# 5. 写入 systemd 服务文件
echo ">>> 配置 systemd 服务..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Feishu Antigravity CLI Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment="PATH=${AGY_DIR}:${UV_DIR}:/usr/local/bin:/usr/bin:/bin"
ExecStart=${UV_BIN} run ${INSTALL_DIR}/feishu_agy_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 6. 重载并设置开机自启
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo ""
echo "================ 部署完成 ================"
echo "1. 编辑配置文件填入飞书凭证："
echo "   nano ${INSTALL_DIR}/.env"
echo "2. 启动服务："
echo "   sudo systemctl restart ${SERVICE_NAME}"
echo "3. 查看实时日志："
echo "   journalctl -u ${SERVICE_NAME} -f"
echo "=========================================="
