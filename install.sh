#!/bin/bash
# =============================================================================
# traffic_routing —— 母机网卡流量监控 一键安装脚本
#
# 用法（无需交互，全部靠环境变量）:
#   bash <(curl -fsSL https://raw.githubusercontent.com/famintt/traffic_routing/main/install.sh)
#
# 带参数:
#   BIND=0.0.0.0:8899 AGENT_TOKEN=xxxx bash <(curl -fsSL .../install.sh)
#   LIMIT=500 INTERVAL=60 bash <(curl -fsSL .../install.sh)
#
# 环境变量:
#   BIND         监听地址            默认 127.0.0.1:8899（仅本机，配合 SSH 隧道）
#   AGENT_TOKEN  平台 agent_token    设置后自动派生鉴权 token（推荐）
#   TOKEN        直接指定 Bearer token（优先于 AGENT_TOKEN）
#   LIMIT        当月流量上限 GB     默认 0（只统计不限制）
#   INTERVAL     采样间隔秒          默认 60
#   REPO_RAW     源码地址前缀        默认 https://raw.githubusercontent.com/famintt/traffic_routing/main
#   INIT         1=强制重置当月累计  默认 0（重装保留数据）
#   NO_SERVICE   1=只装文件不起服务  默认 0
# =============================================================================
set -euo pipefail

PROG="netmon-agent"
VERSION="1.0.0"
INSTALL_DIR="/opt/traffic_routing"
CONF_DIR="/etc/traffic_routing"
STATE_DIR="/var/lib/traffic_monitor"
SERVICE_NAME="netmon-agent"

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/famintt/traffic_routing/main}"
BIND="${BIND:-127.0.0.1:8899}"
LIMIT="${LIMIT:-0}"
INTERVAL="${INTERVAL:-60}"
INIT="${INIT:-0}"
NO_SERVICE="${NO_SERVICE:-0}"
TOKEN="${TOKEN:-}"
AGENT_TOKEN="${AGENT_TOKEN:-}"

C_OK=""  C_WARN=""  C_ERR=""  C_DIM=""  C_RST=""
if [ -t 1 ] && [ "${NO_COLOR:-0}" != "1" ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_DIM=$'\033[2m'; C_RST=$'\033[0m'
fi

ok()   { echo "${C_OK}[OK]${C_RST}   $*"; }
info() { echo "${C_DIM}[..]${C_RST}   $*"; }
warn() { echo "${C_WARN}[警告]${C_RST} $*"; }
die()  { echo "${C_ERR}[失败]${C_RST} $*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash install.sh）"

echo
echo "=============================================="
echo "  ${PROG} ${VERSION}  一键安装"
echo "=============================================="
echo "  监听地址   : ${BIND}"
echo "  采样间隔   : ${INTERVAL}s"
echo "  流量上限   : $( [ "$LIMIT" = "0" ] && echo '不限（仅统计）' || echo "${LIMIT} GB" )"
echo "  鉴权       : $( [ -n "$TOKEN" ] && echo '自定义 token' || { [ -n "$AGENT_TOKEN" ] && echo '由 agent_token 派生' || echo '无（仅回环）'; } )"
echo "=============================================="
echo

# ---------------------------------------------------------------- 依赖
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,6) else 1)' 2>/dev/null; then
            PY="$c"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    info "未找到 python3，正在安装…"
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 curl >/dev/null
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache python3 curl >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3 curl >/dev/null
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q python3 curl >/dev/null
    else
        die "无法自动安装 python3，请先手动安装 python3 (>=3.6)"
    fi
    command -v python3 >/dev/null 2>&1 && PY=python3 || die "python3 安装失败"
fi
ok "python3: $(command -v "$PY") ($("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])'))"

# ---------------------------------------------------------------- 目录
mkdir -p "$INSTALL_DIR" "$CONF_DIR" "$STATE_DIR"
chmod 700 "$CONF_DIR" "$STATE_DIR"
ok "目录就绪: $INSTALL_DIR  $CONF_DIR  $STATE_DIR"

# ---------------------------------------------------------------- 取主程序
fetch_to() {   # fetch_to <url> <outfile>
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --max-time 60 "$1" -o "$2" && return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO "$2" --timeout=60 "$1" && return 0
    fi
    return 1
}

SCRIPT_DIR_SELF=""
[ -n "${BASH_SOURCE[0]:-}" ] && SCRIPT_DIR_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"

if [ -n "$SCRIPT_DIR_SELF" ] && [ -f "$SCRIPT_DIR_SELF/netmon-agent.py" ]; then
    cp "$SCRIPT_DIR_SELF/netmon-agent.py" "$INSTALL_DIR/netmon-agent.py"
    ok "主程序来自本地: $SCRIPT_DIR_SELF/netmon-agent.py"
else
    info "下载主程序…"
    fetch_to "${REPO_RAW}/netmon-agent.py" "$INSTALL_DIR/netmon-agent.py" \
        || die "下载失败: ${REPO_RAW}/netmon-agent.py"
    ok "主程序已下载"
fi
chmod 755 "$INSTALL_DIR/netmon-agent.py"
"$PY" -m py_compile "$INSTALL_DIR/netmon-agent.py" || die "主程序语法检查失败"
rm -rf "$INSTALL_DIR/__pycache__"
ok "主程序校验通过"

# 顺带放一份卸载脚本，方便之后卸载
if [ -n "$SCRIPT_DIR_SELF" ] && [ -f "$SCRIPT_DIR_SELF/uninstall.sh" ]; then
    cp "$SCRIPT_DIR_SELF/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
else
    fetch_to "${REPO_RAW}/uninstall.sh" "$INSTALL_DIR/uninstall.sh" 2>/dev/null \
        || warn "卸载脚本下载失败（不影响使用）"
fi
[ -f "$INSTALL_DIR/uninstall.sh" ] && chmod 755 "$INSTALL_DIR/uninstall.sh"

# ---------------------------------------------------------------- 派生 token
if [ -z "$TOKEN" ] && [ -n "$AGENT_TOKEN" ]; then
    TOKEN="$(cd "$INSTALL_DIR" && "$PY" netmon-agent.py --print-token --token-from-agent "$AGENT_TOKEN")"
    [ -n "$TOKEN" ] || die "token 派生失败"
    ok "已由 agent_token 派生鉴权 token"
fi

HOST_PART="${BIND%:*}"
case "$HOST_PART" in
    127.0.0.1|::1|localhost) NEED_TOKEN=0 ;;
    *) NEED_TOKEN=1 ;;
esac
if [ "$NEED_TOKEN" = "1" ] && [ -z "$TOKEN" ]; then
    die "监听 ${BIND} 是非回环地址，必须提供 TOKEN 或 AGENT_TOKEN。
     例如: BIND=0.0.0.0:8899 AGENT_TOKEN=<母机agent_token> bash install.sh
     或改用默认的 127.0.0.1:8899 配合 SSH 隧道。"
fi

# ---------------------------------------------------------------- 写配置
cat > "$CONF_DIR/agent.json" <<EOF
{
 "bind": "${BIND}",
 "limit_gb": ${LIMIT},
 "interval": ${INTERVAL},
 "auto_block": false,
 "token": "${TOKEN}"
}
EOF
chmod 600 "$CONF_DIR/agent.json"
ok "配置已写入 $CONF_DIR/agent.json"

# ---------------------------------------------------------------- 初始化状态
if [ "$INIT" = "1" ] || [ ! -f "$STATE_DIR/agent-state.json" ]; then
    ( cd "$INSTALL_DIR" && "$PY" netmon-agent.py --init ) >/dev/null
    ok "流量统计已初始化（当月从 0 开始累计）"
else
    ok "保留已有统计数据"
fi

# ---------------------------------------------------------------- 服务
if [ "$NO_SERVICE" = "1" ]; then
    warn "NO_SERVICE=1，跳过服务注册"
else
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=netmon-agent - NIC traffic monitor (traffic_routing)
Documentation=${REPO_RAW%/main}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${PY} ${INSTALL_DIR}/netmon-agent.py
Restart=always
RestartSec=5
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
        systemctl restart "$SERVICE_NAME"
        sleep 1
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            ok "systemd 服务已启动: ${SERVICE_NAME}"
        else
            warn "服务未启动，最后 20 行日志:"
            journalctl -u "$SERVICE_NAME" -n 20 --no-pager 2>/dev/null || true
            die "服务启动失败"
        fi
    elif command -v rc-update >/dev/null 2>&1; then
        cat > "/etc/init.d/${SERVICE_NAME}" <<EOF
#!/sbin/openrc-run
name="${SERVICE_NAME}"
command="${PY}"
command_args="${INSTALL_DIR}/netmon-agent.py"
command_background=true
pidfile="/run/\${RC_SVCNAME}.pid"
output_log="/var/log/${SERVICE_NAME}.log"
error_log="/var/log/${SERVICE_NAME}.log"
depend() { need net; }
EOF
        chmod 755 "/etc/init.d/${SERVICE_NAME}"
        rc-update add "$SERVICE_NAME" default >/dev/null 2>&1 || true
        rc-service "$SERVICE_NAME" restart >/dev/null 2>&1 || rc-service "$SERVICE_NAME" start >/dev/null
        sleep 1
        if rc-service "$SERVICE_NAME" status >/dev/null 2>&1; then
            ok "OpenRC 服务已启动: ${SERVICE_NAME}"
        else
            warn "服务未启动，查看 /var/log/${SERVICE_NAME}.log"
        fi
    else
        warn "未检测到 systemd / OpenRC，跳过服务注册（可手动 nohup 启动）"
    fi
fi

# ---------------------------------------------------------------- 自检
SELF_URL="http://${HOST_PART}:${BIND##*:}/health"
if command -v curl >/dev/null 2>&1; then
    HEALTH="$(curl -fsS --max-time 5 "$SELF_URL" 2>/dev/null || true)"
    if echo "$HEALTH" | grep -q '"ok": *true'; then
        ok "健康检查通过: $SELF_URL"
    else
        warn "健康检查未通过（$SELF_URL），请查看服务日志"
    fi
fi

# ---------------------------------------------------------------- 汇总
echo
echo "=============================================="
echo "  安装完成"
echo "=============================================="
echo "  程序      : ${INSTALL_DIR}/netmon-agent.py"
echo "  配置      : ${CONF_DIR}/agent.json"
echo "  状态      : ${STATE_DIR}/agent-state.json"
echo "  监听      : ${BIND}"
if [ -n "$TOKEN" ]; then
    echo "  Token     : ${TOKEN}"
    echo "  ${C_DIM}(面板用同一 agent_token 现算 HMAC-SHA256(agent_token,'netmon-v1') 即可)${C_RST}"
fi
echo
echo "  常用命令:"
echo "    ${PY} ${INSTALL_DIR}/netmon-agent.py --once     # 立即采样并打印"
if command -v systemctl >/dev/null 2>&1; then
    echo "    systemctl status ${SERVICE_NAME}"
    echo "    journalctl -u ${SERVICE_NAME} -f"
fi
echo "    curl -s http://${BIND}/status | python3 -m json.tool"
echo "    bash ${INSTALL_DIR}/uninstall.sh                 # 卸载"
echo "=============================================="
echo
