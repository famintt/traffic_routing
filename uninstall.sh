#!/bin/bash
# =============================================================================
# traffic_routing —— 卸载脚本
#
#   bash uninstall.sh            # 停服务、删程序，保留流量数据
#   PURGE=1 bash uninstall.sh    # 连配置和流量数据一起删
# =============================================================================
set -uo pipefail

INSTALL_DIR="/opt/traffic_routing"
CONF_DIR="/etc/traffic_routing"
STATE_DIR="/var/lib/traffic_monitor"
SERVICE_NAME="netmon-agent"
PURGE="${PURGE:-0}"

[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }

echo "=== 卸载 ${SERVICE_NAME} ==="

if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    systemctl stop "$SERVICE_NAME" 2>/dev/null
    systemctl disable "$SERVICE_NAME" 2>/dev/null
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload 2>/dev/null
    echo "  systemd 服务已移除"
elif command -v rc-service >/dev/null 2>&1 && [ -f "/etc/init.d/${SERVICE_NAME}" ]; then
    rc-service "$SERVICE_NAME" stop 2>/dev/null
    rc-update del "$SERVICE_NAME" default 2>/dev/null
    rm -f "/etc/init.d/${SERVICE_NAME}"
    echo "  OpenRC 服务已移除"
fi

pkill -f "[n]etmon-agent.py" 2>/dev/null && echo "  残留进程已清理"

# 关键：如果处于断网状态，必须先撤规则再删程序，否则机器永久断网且无人能解
if command -v nft >/dev/null 2>&1 && nft list table inet tr_block >/dev/null 2>&1; then
    nft delete table inet tr_block 2>/dev/null && echo "  已撤销断网规则(nft inet tr_block)"
fi
if command -v iptables >/dev/null 2>&1; then
    for _ in 1 2 3; do iptables -D OUTPUT -j TR_OUT 2>/dev/null || break; done
    for _ in 1 2 3; do iptables -D FORWARD -j TR_FWD 2>/dev/null || break; done
    iptables -F TR_OUT 2>/dev/null; iptables -X TR_OUT 2>/dev/null
    iptables -F TR_FWD 2>/dev/null; iptables -X TR_FWD 2>/dev/null
fi

rm -rf "$INSTALL_DIR"
echo "  程序目录已删除: $INSTALL_DIR"

if [ "$PURGE" = "1" ]; then
    rm -rf "$CONF_DIR" "$STATE_DIR"
    echo "  配置与流量数据已删除"
    echo
    echo "=== 卸载完成（已彻底清除）==="
else
    echo "  保留配置: $CONF_DIR"
    echo "  保留流量数据: $STATE_DIR"
    echo
    echo "=== 卸载完成（数据保留，用 PURGE=1 可彻底删除）==="
fi
