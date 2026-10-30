#!/bin/bash
set -u
cd /opt/traffic_routing

echo "=== 启动 agent (127.0.0.1:8899) ==="
python3 netmon-agent.py --bind 127.0.0.1:8899 --interval 10 >/tmp/agent.log 2>&1 &
APID=$!
sleep 2

echo "--- /health (无鉴权) ---"
curl -s --max-time 5 http://127.0.0.1:8899/health; echo

echo "--- /status ---"
curl -s --max-time 5 http://127.0.0.1:8899/status; echo

echo "--- /config ---"
curl -s --max-time 5 http://127.0.0.1:8899/config; echo

echo "--- 制造点流量 (下载 1MB) 后重新采样 ---"
curl -s -o /dev/null --max-time 20 https://speed.cloudflare.com/__down?bytes=1000000 || true
sleep 1
curl -s --max-time 5 http://127.0.0.1:8899/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('rx=%d tx=%d used=%d'%(d['rx_bytes'],d['tx_bytes'],d['used_bytes']))"

echo "--- PUT /config 设置上限 500GB ---"
curl -s -X PUT --max-time 5 -H 'Content-Type: application/json' \
  -d '{"limit_gb":500}' http://127.0.0.1:8899/config; echo

echo "--- 非法值校验 ---"
curl -s -X PUT --max-time 5 -H 'Content-Type: application/json' \
  -d '{"limit_gb":-5}' http://127.0.0.1:8899/config; echo

echo "--- POST /reset ---"
curl -s -X POST --max-time 5 http://127.0.0.1:8899/reset; echo

echo "--- 重置后 status ---"
curl -s --max-time 5 http://127.0.0.1:8899/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('rx=%d tx=%d percent=%s limit=%s'%(d['rx_bytes'],d['tx_bytes'],d['percent'],d['limit_gb']))"

echo
echo "=== 测试公网模式必须带 token ==="
python3 netmon-agent.py --bind 0.0.0.0:8898 --interval 10 >/tmp/agent2.log 2>&1 &
P2=$!
sleep 1
kill $P2 2>/dev/null
echo "[无 token 启动输出]"; cat /tmp/agent2.log

echo
echo "=== 带派生 token 启动 ==="
python3 netmon-agent.py --bind 0.0.0.0:8898 --token-from-agent 5644fe2a-dc5b-451e-bcba-97e392f21469 --interval 10 >/tmp/agent3.log 2>&1 &
P3=$!
sleep 2
echo "[日志]"; cat /tmp/agent3.log
TOK=$(python3 netmon-agent.py --print-token --token-from-agent 5644fe2a-dc5b-451e-bcba-97e392f21469)
echo "[派生 token] $TOK"
echo "--- 无 token 访问 /status (应 401) ---"
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 5 http://127.0.0.1:8898/status
echo "--- 带 token 访问 /status ---"
curl -s --max-time 5 -H "Authorization: Bearer $TOK" http://127.0.0.1:8898/status | head -c 200; echo

kill $APID $P3 2>/dev/null
wait 2>/dev/null
echo "=== done ==="
