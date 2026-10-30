# traffic_routing

**母机网卡流量统计** —— 读 `/proc/net/dev` 内核累计计数器，按月累计上下行流量，对外提供 HTTP API，供控制面板接入。

> 解决什么问题：托管平台 API 通常只给**瞬时速率**（`net_in_bps` / `net_out_bps`），那是采样值，面板侧积分只能估算，且面板一关就丢数。
> `/proc/net/dev` 是**内核累计计数器**，精确、不受面板开关机影响。

算法与 [哪吒探针 nezha](https://github.com/nezhahq/nezha) 一致，实现思路参考 [jyucoeng/gcp_traffic_routing](https://github.com/jyucoeng/gcp_traffic_routing) 的 `netstat.sh`，但去掉了它「停用 `ufw`/`firewalld`」的副作用，改为**可选、独立、可逆**的断网（见第四节），并补上了 **HTTP API + systemd 服务 + 进程内定时采样**。

---

## 一、一键安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/famintt/traffic_routing/main/install.sh)
```

无 `curl` 的精简系统：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/famintt/traffic_routing/main/install.sh)
```

自动回退两种下载方式：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/famintt/traffic_routing/main/install.sh 2>/dev/null || wget -qO- https://raw.githubusercontent.com/famintt/traffic_routing/main/install.sh)
```

装完默认：

| 项 | 值 |
|---|---|
| 监听 | `127.0.0.1:8899`（**只本机**，公网访问不到） |
| 采样 | 每 60 秒，进程内定时，**不依赖 cron** |
| 上限 | 0（只统计，不做任何拦截） |
| 自启 | systemd / OpenRC，挂了自动重启 |
| 防火墙 | **完全不碰** |

验证：

```bash
curl -s http://127.0.0.1:8899/status | python3 -m json.tool
```

---

## 二、带参数安装（全部环境变量，无需交互）

```bash
BIND=0.0.0.0:8899 AGENT_TOKEN=<母机agent_token> LIMIT=500 bash <(curl -fsSL .../install.sh)
```

| 变量 | 说明 | 默认 |
|---|---|---|
| `BIND` | 监听地址 | `127.0.0.1:8899` |
| `AGENT_TOKEN` | 平台母机 `agent_token`，**设了它就不用手工编 token** | 空 |
| `TOKEN` | 直接指定 Bearer token（优先于 `AGENT_TOKEN`） | 空 |
| `LIMIT` | 当月流量上限 GB（可带小数，如 `10137.6` = 9.9 TB） | `0`（不限） |
| `LIMIT_DIR` | 上限按哪个口径算：`sum` 上下行合计 / `tx` 仅出站 / `rx` 仅入站 | `sum` |
| `AUTO_BLOCK` | `1` = 超限自动断网（会真的掐断业务流量，见第四节） | `0`（只告警不断网） |
| `BLOCK_TTL` | 断网后多少秒自动解除，`0` = 永久（试跑时设 `120` 当保险丝） | `0` |
| `SSH_PORTS` | 断网时保留的端口，逗号分隔（**不保留就再也连不上**） | `22` |
| `INTERVAL` | 采样间隔秒（5~3600） | `60` |
| `REPO_RAW` | 源码地址前缀（自建镜像时用） | 见脚本头 |
| `INIT` | `1` = 强制把当月累计清零重计 | `0`（重装保留数据） |
| `NO_SERVICE` | `1` = 只放文件，不注册服务 | `0` |

示例：

```bash
# 只统计不限制，默认安全模式
bash <(curl -fsSL .../install.sh)

# 公网模式（面板直连），用母机自带的 agent_token 派生鉴权
BIND=0.0.0.0:8899 AGENT_TOKEN=5644fe2a-xxxx-xxxx-xxxx-xxxxxxxxxxxx bash <(curl -fsSL .../install.sh)

# 有上限，面板上会显示进度条
LIMIT=500 bash <(curl -fsSL .../install.sh)

# 超限自动断网：9.9 TB 上限，超了就掐网，但保留 22 端口救命
LIMIT=10137.6 AUTO_BLOCK=1 bash <(curl -fsSL .../install.sh)

# 试跑断网逻辑：断 2 分钟后自动恢复
LIMIT=1 AUTO_BLOCK=1 BLOCK_TTL=120 bash <(curl -fsSL .../install.sh)

# 采样更密（20 秒）
INTERVAL=20 bash <(curl -fsSL .../install.sh)
```

---

## 三、HTTP API

除 `/health` 外都需要 `Authorization: Bearer <token>`（**仅当监听非回环地址时**；回环地址由 SSH 隧道保护，不需要 token）。

### `GET /health`

不需要鉴权，用于面板「测试连接」。

```json
{"ok": true, "version": "1.1.0", "service": "netmon-agent"}
```

### `GET /status`

```json
{
  "ok": true,
  "version": "1.1.0",
  "hostname": "sg-host-01",
  "month": "2026-10",
  "rx_bytes": 573451222,
  "tx_bytes": 621187330,
  "used_bytes": 1194638552,
  "metric_bytes": 1194638552,
  "limit_bytes": 10884533411840,
  "limit_gb": 10137.6,
  "limit_dir": "sum",
  "percent": 0.01,
  "updated": 1793341391.88,
  "interval": 60,
  "ifaces": [
    {"name": "enp0s3", "rx": 9182736455, "tx": 7263546271}
  ],
  "archive": {"2026-09": {"rx": 812345678, "tx": 901234567}},
  "uptime": 842891,
  "fw": "nft",
  "auto_block": true,
  "blocked": false,
  "blocked_at": 0,
  "block_reason": "",
  "block_expire": 0,
  "block_ttl": 0,
  "ssh_ports": "22"
}
```

- `rx_bytes` / `tx_bytes`：**当月**累计，跨月自动归零并把上月写进 `archive`
- `used_bytes`：`rx_bytes + tx_bytes`（展示用，与上游 `sum` 计费口径一致）
- `metric_bytes`：按 `limit_dir` 取出的**判定值**（`sum` 时等于 `used_bytes`）
- `percent`：`metric_bytes / limit_bytes`（`limit_gb=0` 时为 0）
- `ifaces`：参与统计的物理网卡及其**开机以来**累计值（调试用）
- `fw`：可用的断网后端 `nft` / `iptables` / `none`
- `blocked` / `block_reason`：当前是否处于断网状态，原因 `auto`（超限）/`manual`（手动）/`api`/`ttl`
- `block_expire`：非 0 表示到这个 Unix 时间戳会自动解除断网

### `GET /config`

```json
{"ok": true, "config": {"limit_gb": 10137.6, "limit_dir": "sum", "interval": 60,
 "auto_block": true, "block_ttl": 0, "ssh_ports": "22", "bind": "127.0.0.1:8899"}}
```

### `PUT /config`

```bash
curl -X PUT http://127.0.0.1:8899/config \
     -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
     -d '{"limit_gb": 10137.6, "limit_dir": "tx", "auto_block": true}'
```

可改字段：`limit_gb`（≥0）、`interval`（5~3600）、`limit_dir`（`sum|rx|tx`）、
`auto_block`（布尔）、`block_ttl`（≥0）、`ssh_ports`（如 `"22,2222"`）。非法值返回 400。

### `POST /reset`

把当月累计清零（快照重置为当前值，之后正常累加）。

```bash
curl -X POST http://127.0.0.1:8899/reset -H 'Authorization: Bearer <token>'
```

### `POST /block` / `POST /unblock`

立即断网 / 解除断网。`/block` 可带 `{"reason": "..."}`。

```bash
curl -X POST http://127.0.0.1:8899/block   -H 'Authorization: Bearer <token>' \
     -H 'Content-Type: application/json' -d '{"reason":"客户欠费"}'
curl -X POST http://127.0.0.1:8899/unblock -H 'Authorization: Bearer <token>'
```

返回 `{"ok": true, "msg": "...", "blocked": true}`。

---

## 四、超限自动断网（v1.1.0）

`AUTO_BLOCK=1` 时，当月流量达到 `LIMIT` 就**真的掐网**：断掉母机上的转发流量，
同时留一条 SSH 生命线，保证你随时能进去解封。

### 断的是什么

只建一个独立表，不碰机器上任何已有防火墙规则：

```
table inet tr_block {              # 没有 nft 则退回 iptables 独立链 TR_FWD / TR_OUT
    chain ch_fwd {                 # 断掉所有转发（= 所有容器的进出流量）
        type filter hook forward priority 10; policy accept;
        counter drop
    }
    chain ch_out {                 # 断掉母机自身出站，除了下面三条
        type filter hook output priority 10; policy accept;
        oifname "lo" accept        # 回环：agent 的 :8899、SSH 隧道照常可用
        tcp sport 22 accept        # SSH 回包
        tcp dport 22 accept        # SSH 出站（含隧道转发）
        counter drop
    }
}
```

- **删表 = 完全恢复**，不留痕迹：`nft delete table inet tr_block`
- 建表走**单次 nft 事务**，语法或权限有问题时整批回滚，不会留下半套规则把自己锁死
- 建完**自检**：表里找不到 `ssh_ports` 的白名单就立即回滚并放弃断网
- `ssh_ports` 默认 `22`，写 `22,2222` 就同时保留自定义端口

> 链名不能叫 `fwd` / `out` —— 这两个是 nft 的保留字，加引号也没用，只能换名（`ch_fwd` / `ch_out`）。

### 怎么解封

任选其一：

```bash
python3 /opt/traffic_routing/netmon-agent.py --unblock
curl -X POST http://127.0.0.1:8899/unblock     # 走 SSH 隧道时
```

跨月后累计归零，**`auto` 触发的断网会自动解除**（手动断网不会被自动解除）。

### 保险丝 `BLOCK_TTL`

试跑时设 `BLOCK_TTL=120`：断网 2 分钟后自动恢复。生产用默认 `0`（永久），
否则到点自动恢复等于没断。

### 三个安全兜底

1. **启动自检**：agent 重启后比对「状态文件里的 blocked」与「防火墙实际情况」，以实际为准
2. **卸载自动解封**：`uninstall.sh` 先 `nft delete table inet tr_block` 再删程序
3. **失败不改状态**：防火墙没生效就不写 `blocked=true`，避免面板显示已断网而实际没断

### 口径别搞错

`LIMIT` 单位是 **GB**，9.9 TB 要写 `10137.6`。判定方向由 `LIMIT_DIR` 决定：

| `LIMIT_DIR` | 含义 | 什么时候用 |
|---|---|---|
| `sum`（默认） | 上下行相加 | 与平台 `sum` 计费口径一致 |
| `tx` | 只看出站 | 云厂商按出站计费（Oracle 免额度常见） |
| `rx` | 只看入站 | 少见 |

---

## 五、鉴权：不新增任何凭据

公网监听时，token **由平台 `agent_token` 派生**，不额外发一个密码：

```python
token = hmac_sha256(agent_token, b"netmon-v1").hexdigest()
```

- 母机：安装时传 `AGENT_TOKEN=...`，脚本现算并写进 `agent.json`（权限 600）
- 面板：从平台 API 拿到同一台母机的 `agent_token`，本地现算，**不需要存第二份密钥**
- 为什么不直接用 `agent_token`：它是平台凭据，泄漏后可冒充母机上报。派生之后即使泄漏，也换不回平台 token

命令行也能算：

```bash
python3 /opt/traffic_routing/netmon-agent.py --print-token --token-from-agent <agent_token>
```

---

## 六、面板接入：推荐用 SSH 隧道

`127.0.0.1:8899` 公网访问不到，最安全的接法是面板建 SSH 隧道：

```bash
# 面板侧执行（或由面板用 paramiko 自动建）
ssh -N -L 8899:127.0.0.1:8899 root@161.118.194.102
# 之后面板访问 http://127.0.0.1:8899/status 就等于访问母机本地
```

好处：**公网上不多开一个端口**，母机是生产机，这点很重要。

如果面板和母机之间没有 SSH，才用公网模式：

```bash
BIND=0.0.0.0:8899 AGENT_TOKEN=<agent_token> bash install.sh
```

此时**务必**：
- 用强 token（派生值本身就是 64 位十六进制）
- 只暴露只读接口（本项目的 `PUT`/`POST` 需要同一 token，能读就能写，注意）
- 有条件的话加一条云安全组/防火墙，只放行面板的出口 IP

---

## 七、与 `gcp_traffic_routing` 的区别

| | gcp_traffic_routing | 本项目 |
|---|---|---|
| 统计方式 | 读 `/proc/net/dev`，cron 每 5 分钟 | 同样读 `/proc/net/dev`，**进程内定时**，不依赖 cron |
| 对外接口 | 无（只有本地文件 + 命令行菜单） | **HTTP API**（status/config/reset） |
| 配置方式 | SSH 进去跑交互菜单 `bash traffic_ctrl.sh edit` | **面板远程设置** |
| 超限行为 | 强制封网 + TG 通知 | 默认只统计+告警；`AUTO_BLOCK=1` 才断网，**独立表、保留 SSH** |
| 防火墙副作用 | `PLATFORM=oracle` 会停 `firewalld`/`ufw` | **不碰任何已有规则**，只建自己的表/链 |
| 部署 | 交互式 | **纯环境变量，零交互** |

在母机上乱动防火墙对生产托管机风险极高（一个误判就是 45 个客户断网），所以本项目的断网功能是
**显式开关 + 独立表 + 保留 SSH + 可设 TTL**，默认关闭。

---

## 八、常用命令

```bash
# 立即采样并打印
python3 /opt/traffic_routing/netmon-agent.py --once

# 初始化（当月清零，快照=当前）
python3 /opt/traffic_routing/netmon-agent.py --init

# 服务管理
systemctl status netmon-agent
systemctl restart netmon-agent
journalctl -u netmon-agent -f

# 卸载（保留数据）
bash /opt/traffic_routing/uninstall.sh
# 彻底清除
PURGE=1 bash /opt/traffic_routing/uninstall.sh
```

---

## 九、文件位置

| 路径 | 作用 |
|---|---|
| `/opt/traffic_routing/netmon-agent.py` | 主程序（单文件，纯标准库） |
| `/opt/traffic_routing/uninstall.sh` | 卸载脚本 |
| `/etc/traffic_routing/agent.json` | 配置（0600） |
| `/var/lib/traffic_monitor/agent-state.json` | 流量状态：当月累计 + 上次快照 + 月度归档 |
| `/etc/systemd/system/netmon-agent.service` | systemd 单元（或 `/etc/init.d/netmon-agent`） |

状态文件长这样：

```json
{
 "month": "2026-10",
 "rx": 573451222,
 "tx": 621187330,
 "last_rx": 9182736455,
 "last_tx": 7263546271,
 "updated": 1793341391.88,
 "archive": {"2026-09": {"rx": 812345678, "tx": 901234567}}
}
```

---

## 十、已知限制

- **无法回溯历史**：从安装那一刻开始统计。想要更早的数据只能用平台 API 的实例累加值。
- **计数器归零会少算一段**：主机重启后 `/proc/net/dev` 从 0 开始，重启前最后一段增量（最多一个采样间隔）会丢失。与 nezha 行为一致。
- **只统计物理网卡**：`lo` / `docker` / `veth` / `br-` / `virbr` / `tun` / `tap` / `vbox` / `dummy` / `podman` / `cni` / `cali` 等已被排除，避免容器内外重复计数。
- **月界按母机本地时区**：Oracle/GCP 多为 UTC，跨月时刻与北京时间差 8 小时。

## 十一、License

MIT
