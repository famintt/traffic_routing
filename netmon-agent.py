#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netmon-agent —— 母机网卡流量监控本地服务

算法与哪吒探针 (nezha) 一致：
    读 /proc/net/dev 的内核累计计数器
      -> 排除虚拟网卡 (lo / docker / veth / br- / virbr / tun / tap / vbox / dummy)
      -> 对剩余物理网卡分别求 RX(下行) / TX(上行) 总和
      -> 与上次快照求差得到增量
      -> 累加到当月 RX / TX
      -> 跨月自动归档并归零

为什么要这么做：
    上游 API 的 reported_specs.net_in_bps / net_out_bps 是**瞬时速率采样值**，
    面板侧积分只能估算；/proc/net/dev 是**内核累计计数器**，精确无误差。

对外 HTTP API（默认只监听 127.0.0.1:8899）：
    GET  /health    健康检查（不需要鉴权，用于面板测试连接）
    GET  /status    当月累计流量 + 网卡明细
    GET  /config    当前配置
    PUT  /config    修改配置（limit_gb / interval）
    POST /reset     清零当月累计

仅在监听非回环地址时才强制鉴权（Bearer token）。回环地址由 SSH 隧道保护，不需要 token。
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"
PROG = "netmon-agent"

CONF_DIR = "/etc/traffic_routing"
CONF_FILE = os.path.join(CONF_DIR, "agent.json")
STATE_DIR = "/var/lib/traffic_monitor"
STATE_FILE = os.path.join(STATE_DIR, "agent-state.json")
PROC_NET_DEV = "/proc/net/dev"

# 虚拟网卡前缀（与 nezha / gcp_traffic_routing 保持一致）
VIRTUAL_RE = re.compile(
    r"^(lo|docker|veth|br-|virbr|tun|tap|vbox|dummy|podman|cni|flannel|cali|"
    r"kube|nerdctl|vxlan|lxc|lxd|wg|zt)"
)

DEFAULT_CONF = {
    "limit_gb": 0,          # 0 = 不限制（仅统计）
    "limit_dir": "sum",     # 计费方向: sum(上下行合计) / tx(仅出站) / rx(仅入站)
    "interval": 60,         # 采样间隔秒
    "auto_block": False,    # 超限自动断网
    "block_ttl": 0,         # 断网后多少秒自动解除（0=永久，用于生产；>0 用于试跑/防误封）
    "ssh_ports": "22",      # 断网时保留的端口（逗号分隔），防止把自己锁在外面
    "token": "",            # 非回环监听时必须非空
    "bind": "127.0.0.1:8899",
}

log = logging.getLogger(PROG)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            merged = dict(default)
            merged.update(data or {})
            return merged
        return data
    except (OSError, ValueError):
        return dict(default) if isinstance(default, dict) else default


def save_json(path, data):
    """原子写入，避免进程被杀时留下半个文件。

    写临时文件后 chmod 600 再 rename —— 否则每次重写都会把
    安装脚本设好的 0600 权限（保护 token）冲掉。
    """
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def month_key(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return "%.2f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def derive_token(agent_token):
    """
    由平台 agent_token 派生本服务用的子密钥。

    为什么不直接用 agent_token：
        agent_token 是 narwhal 平台凭据，泄漏后可冒充母机上报。
        派生后即使泄漏，攻击者也换不回平台 token。
    面板侧用同一算法现算，双方都不需要额外保存密钥。
    """
    return hmac.new(agent_token.encode("utf-8"), b"netmon-v1", hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# 防火墙：超限断网
#
# 设计原则（最重要的一条）：绝不改动机器上已有的任何防火墙规则。
#   nft      → 只建一个独立表 inet tr_block，删表即完全复原
#   iptables → 只建独立链 TR_FWD / TR_OUT，删链即完全复原
# 断网时**永远保留 SSH 端口**，否则会把自己锁在门外。
# 建表用一次性事务提交，语法错误时 nft 会整批回滚，不会留下半套规则。
# --------------------------------------------------------------------------
FW_TABLE = "tr_block"
FW_CHAIN_FWD = "ch_fwd"     # fwd / out 是 nft 保留字，链名只能另起
FW_CHAIN_OUT = "ch_out"
IPT_CHAIN_FWD = "TR_FWD"
IPT_CHAIN_OUT = "TR_OUT"


def fw_backend():
    if shutil.which("nft"):
        return "nft"
    if shutil.which("iptables"):
        return "iptables"
    return ""


def _run(cmd, payload=None, timeout=25):
    try:
        p = subprocess.run(cmd, input=payload, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace").strip(),
                p.stderr.decode("utf-8", "replace").strip())
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def parse_ports(s):
    """'22,2222,10000-10010' -> ['22','2222','10000-10010']"""
    out = []
    for part in str(s or "").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.append("%d-%d" % (int(a), int(b)))
            continue
        if part.isdigit():
            out.append(str(int(part)))
    return out or ["22"]


def _nft_script(ports):
    # 链名不能叫 fwd / out —— 这两个在 nft 里是保留字（fwd 是语句关键字），
    # 会报 "unexpected fwd, expecting string or last"，加引号也没用，只能换名。
    body = [
        "table inet %s {" % FW_TABLE,
        "    chain %s {" % FW_CHAIN_FWD,
        "        type filter hook forward priority 10; policy accept;",
        "        counter drop",
        "    }",
        "    chain %s {" % FW_CHAIN_OUT,
        "        type filter hook output priority 10; policy accept;",
        '        oifname "lo" accept',
    ]
    for p in ports:
        body.append("        tcp sport %s accept" % p)
        body.append("        tcp dport %s accept" % p)
    body += [
        "        counter drop",
        "    }",
        "}",
    ]
    return "\n".join(body) + "\n"


def _iptables_cleanup():
    for hook, chain in (("OUTPUT", IPT_CHAIN_OUT), ("FORWARD", IPT_CHAIN_FWD)):
        for _ in range(5):
            rc, _, _ = _run(["iptables", "-D", hook, "-j", chain])
            if rc != 0:
                break
        _run(["iptables", "-F", chain])
        _run(["iptables", "-X", chain])


def fw_block(ports=("22",)):
    """建立断网规则，返回 (ok, msg)。失败时保证不残留半套规则。"""
    be = fw_backend()
    if be == "nft":
        pre = ""
        rc, _, _ = _run(["nft", "list", "table", "inet", FW_TABLE])
        if rc == 0:
            pre = "delete table inet %s\n" % FW_TABLE
        script = pre + _nft_script(ports)
        rc, out, err = _run(["nft", "-f", "-"], payload=script.encode())
        if rc != 0:
            return False, "nft 应用失败: %s" % (err or out)
        rc, listing, _ = _run(["nft", "list", "table", "inet", FW_TABLE])
        if rc != 0:
            return False, "nft 表校验失败"
        missing = []
        for p in ports:
            if "tcp sport %s" % p not in listing:
                missing.append("sport " + p)
            if "tcp dport %s" % p not in listing:
                missing.append("dport " + p)
        if missing:
            _run(["nft", "delete", "table", "inet", FW_TABLE])
            return False, "自检失败(%s)，已回滚，未断网" % ",".join(missing)
        return True, "nft 表 inet %s 已生效，保留端口 %s" % (FW_TABLE, ",".join(ports))

    if be == "iptables":
        _iptables_cleanup()
        seq = [["iptables", "-N", IPT_CHAIN_OUT],
               ["iptables", "-A", IPT_CHAIN_OUT, "-o", "lo", "-j", "ACCEPT"]]
        for p in ports:
            seq.append(["iptables", "-A", IPT_CHAIN_OUT, "-p", "tcp", "--sport", p, "-j", "ACCEPT"])
            seq.append(["iptables", "-A", IPT_CHAIN_OUT, "-p", "tcp", "--dport", p, "-j", "ACCEPT"])
        seq += [["iptables", "-A", IPT_CHAIN_OUT, "-j", "DROP"],
                ["iptables", "-I", "OUTPUT", "1", "-j", IPT_CHAIN_OUT],
                ["iptables", "-N", IPT_CHAIN_FWD],
                ["iptables", "-A", IPT_CHAIN_FWD, "-j", "DROP"],
                ["iptables", "-I", "FORWARD", "1", "-j", IPT_CHAIN_FWD]]
        for cmd in seq:
            rc, out, err = _run(cmd)
            if rc != 0:
                _iptables_cleanup()
                return False, "iptables 失败(%s): %s" % (" ".join(cmd), err or out)
        return True, "iptables 链 %s/%s 已生效" % (IPT_CHAIN_OUT, IPT_CHAIN_FWD)

    return False, "系统既没有 nft 也没有 iptables，无法断网"


def fw_unblock():
    """撤销断网，返回 (ok, msg)。本身没断网也算成功。"""
    be = fw_backend()
    if be == "nft":
        rc, out, err = _run(["nft", "delete", "table", "inet", FW_TABLE])
        if rc == 0:
            return True, "nft 表 inet %s 已删除" % FW_TABLE
        blob = (err + out).lower()
        if "no such file" in blob or "does not exist" in blob:
            return True, "当前没有断网规则"
        return False, "nft 删除失败: %s" % (err or out)
    if be == "iptables":
        _iptables_cleanup()
        return True, "iptables 链已清除"
    return False, "系统既没有 nft 也没有 iptables"


def fw_active():
    be = fw_backend()
    if be == "nft":
        rc, _, _ = _run(["nft", "list", "table", "inet", FW_TABLE])
        return rc == 0
    if be == "iptables":
        rc, _, _ = _run(["iptables", "-C", "OUTPUT", "-j", IPT_CHAIN_OUT])
        return rc == 0
    return False


def limit_bytes_of(conf):
    try:
        gb = float(conf.get("limit_gb") or 0)
    except (TypeError, ValueError):
        gb = 0.0
    return int(gb * 1024 ** 3) if gb > 0 else 0


def metric_bytes(st, conf):
    """按 limit_dir 取用于判定的字节数。"""
    d = str(conf.get("limit_dir") or "sum").lower()
    rx = int(st.get("rx", 0))
    tx = int(st.get("tx", 0))
    if d == "tx":
        return tx
    if d == "rx":
        return rx
    return rx + tx


def do_block(state, conf, reason="manual"):
    ports = parse_ports(conf.get("ssh_ports"))
    ok, msg = fw_block(ports)
    if not ok:
        log.error("断网失败: %s", msg)
        return False, msg
    st = state.snapshot()
    now = time.time()
    st["blocked"] = True
    st["blocked_at"] = now
    st["block_reason"] = reason
    try:
        ttl = int(conf.get("block_ttl") or 0)
    except (TypeError, ValueError):
        ttl = 0
    st["block_expire"] = (now + ttl) if ttl > 0 else 0
    state.save(st)
    log.warning("已断网 [%s] %s", reason, msg)
    return True, msg


def do_unblock(state, reason="manual"):
    ok, msg = fw_unblock()
    if not ok:
        log.error("解除断网失败: %s", msg)
        return False, msg
    st = state.snapshot()
    st["blocked"] = False
    st["blocked_at"] = 0
    st["block_reason"] = ""
    st["block_expire"] = 0
    state.save(st)
    log.warning("已解除断网 [%s] %s", reason, msg)
    return True, msg


def evaluate_block(state, st, conf):
    """每次采样后判定：是否该断网 / 该自动恢复。"""
    if not conf.get("auto_block"):
        return
    lb = limit_bytes_of(conf)
    if lb <= 0:
        return
    used = metric_bytes(st, conf)
    blocked = bool(st.get("blocked"))
    if used >= lb and not blocked:
        log.warning("流量超限: %s >= %s，触发自动断网",
                    human_bytes(used), human_bytes(lb))
        do_block(state, conf, "auto")
    elif used < lb and blocked and st.get("block_reason") == "auto":
        do_unblock(state, "auto-recover")


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------
def read_counters():
    """读 /proc/net/dev，返回 (rx_total, tx_total, ifaces)。"""
    rx_total = 0
    tx_total = 0
    ifaces = []
    try:
        with open(PROC_NET_DEV, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("读取 %s 失败: %s", PROC_NET_DEV, e)
        return 0, 0, []

    for line in lines:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if not name or VIRTUAL_RE.match(name):
            continue
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            rx = int(parts[0])   # RX bytes
            tx = int(parts[8])   # TX bytes
        except ValueError:
            continue
        ifaces.append({"name": name, "rx": rx, "tx": tx})
        rx_total += rx
        tx_total += tx

    ifaces.sort(key=lambda x: x["name"])
    return rx_total, tx_total, ifaces


class State(object):
    """流量状态，线程安全。"""

    def __init__(self, path=STATE_FILE, month_rollover_cb=None):
        self.path = path
        self.lock = threading.Lock()
        self.month_rollover_cb = month_rollover_cb

    def _load(self):
        return load_json(self.path, {
            "month": "",
            "rx": 0,
            "tx": 0,
            "last_rx": 0,
            "last_tx": 0,
            "updated": 0,
            "archive": {},
        })

    def snapshot(self):
        with self.lock:
            return self._load()

    def save(self, st):
        with self.lock:
            save_json(self.path, st)

    def init(self):
        """初始化：把快照设成当前值，当月累计清零。用于首次安装。"""
        rx, tx, _ = read_counters()
        with self.lock:
            st = self._load()
            st.update({
                "month": month_key(),
                "rx": 0,
                "tx": 0,
                "last_rx": rx,
                "last_tx": tx,
                "updated": time.time(),
            })
            save_json(self.path, st)
            return st

    def reset(self):
        return self.init()

    def sample(self):
        """采样一次并累加，返回 (state, ifaces)。"""
        rx, tx, ifaces = read_counters()
        now = time.time()
        mk = month_key(now)
        with self.lock:
            st = self._load()
            old_month = st.get("month") or ""

            # 跨月：归档上月并归零（快照保留，作为下次 delta 基准）
            if old_month != mk:
                if old_month:
                    arch = st.get("archive") or {}
                    arch[old_month] = {"rx": st.get("rx", 0), "tx": st.get("tx", 0)}
                    st["archive"] = arch
                    log.info("跨月归档 %s -> rx=%s tx=%s", old_month,
                             human_bytes(st.get("rx", 0)), human_bytes(st.get("tx", 0)))
                st["month"] = mk
                st["rx"] = 0
                st["tx"] = 0

            last_rx = st.get("last_rx") or 0
            last_tx = st.get("last_tx") or 0

            # 计数器回绕 / 主机重启：cur < last 说明归零了，按 nezha 语义把 cur 当增量
            # （这样至少能捕获重启后的流量，不会漏；代价是重启前最后一段增量丢失）
            if last_rx == 0 or rx < last_rx:
                delta_rx = rx
            else:
                delta_rx = rx - last_rx
            if last_tx == 0 or tx < last_tx:
                delta_tx = tx
            else:
                delta_tx = tx - last_tx

            st["rx"] = st.get("rx", 0) + delta_rx
            st["tx"] = st.get("tx", 0) + delta_tx
            st["last_rx"] = rx
            st["last_tx"] = tx
            st["updated"] = now
            save_json(self.path, st)
            return st, ifaces


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
def make_status(st, ifaces, conf):
    rx = int(st.get("rx", 0))
    tx = int(st.get("tx", 0))
    limit_gb = float(conf.get("limit_gb") or 0)
    lb = limit_bytes_of(conf)
    metric = metric_bytes(st, conf)
    pct = round(metric * 100.0 / lb, 2) if lb else 0.0
    return {
        "ok": True,
        "version": VERSION,
        "hostname": socket.gethostname(),
        "month": st.get("month", ""),
        "rx_bytes": rx,
        "tx_bytes": tx,
        "used_bytes": rx + tx,          # 上下行合计（展示用）
        "metric_bytes": metric,         # 按 limit_dir 取出的判定值
        "limit_bytes": lb,
        "limit_gb": limit_gb,
        "limit_dir": str(conf.get("limit_dir") or "sum"),
        "percent": pct,
        "updated": st.get("updated", 0),
        "interval": conf.get("interval", 60),
        "ifaces": ifaces,
        "archive": st.get("archive", {}),
        "uptime": _uptime(),
        # 断网相关
        "fw": fw_backend() or "none",
        "auto_block": bool(conf.get("auto_block")),
        "blocked": bool(st.get("blocked")),
        "blocked_at": st.get("blocked_at", 0),
        "block_reason": st.get("block_reason", ""),
        "block_expire": st.get("block_expire", 0),
        "block_ttl": conf.get("block_ttl", 0),
        "ssh_ports": str(conf.get("ssh_ports") or "22"),
    }


def _uptime():
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError):
        return 0


class Handler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (PROG, VERSION)
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self):
        if self.server.no_auth:
            return True
        want = self.server.token
        if not want:
            return False
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return False
        return hmac.compare_digest(got[7:].strip(), want)

    def _require_auth(self):
        if self._authed():
            return True
        self._send(401, {"ok": False, "error": "unauthorized"})
        return False

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/health", "/healthz"):
            self._send(200, {"ok": True, "version": VERSION, "service": PROG})
            return

        if not self._require_auth():
            return

        if path in ("/", "/status"):
            st, ifaces = self.server.state.sample()
            self._send(200, make_status(st, ifaces, self.server.conf))
        elif path == "/config":
            self._send(200, {"ok": True, "config": self.server.public_conf()})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_PUT(self):
        path = self.path.split("?")[0]
        if not self._require_auth():
            return
        if path != "/config":
            self._send(404, {"ok": False, "error": "not found"})
            return
        body = self._read_json()
        conf = self.server.conf
        changed = {}
        if "limit_gb" in body:
            try:
                v = float(body["limit_gb"])
                if v < 0:
                    raise ValueError
                conf["limit_gb"] = v
                changed["limit_gb"] = v
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "limit_gb must be a non-negative number"})
                return
        if "interval" in body:
            try:
                v = int(body["interval"])
                if not 5 <= v <= 3600:
                    raise ValueError
                conf["interval"] = v
                changed["interval"] = v
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "interval must be 5..3600"})
                return
        if "auto_block" in body:
            v = body["auto_block"]
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on")
            conf["auto_block"] = bool(v)
            changed["auto_block"] = conf["auto_block"]
        if "block_ttl" in body:
            try:
                v = int(body["block_ttl"])
                if v < 0:
                    raise ValueError
                conf["block_ttl"] = v
                changed["block_ttl"] = v
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "block_ttl must be >= 0"})
                return
        if "limit_dir" in body:
            v = str(body["limit_dir"]).strip().lower()
            if v not in ("sum", "rx", "tx"):
                self._send(400, {"ok": False, "error": "limit_dir must be sum|rx|tx"})
                return
            conf["limit_dir"] = v
            changed["limit_dir"] = v
        if "ssh_ports" in body:
            ports = parse_ports(body["ssh_ports"])
            conf["ssh_ports"] = ",".join(ports)
            changed["ssh_ports"] = conf["ssh_ports"]
        if not changed:
            self._send(400, {"ok": False, "error": "nothing to update"})
            return
        save_json(CONF_FILE, conf)
        self.server.apply_conf()
        log.info("配置已更新: %s", changed)
        self._send(200, {"ok": True, "changed": changed, "config": self.server.public_conf()})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._require_auth():
            return
        if path == "/reset":
            st = self.server.state.reset()
            log.info("当月累计已清零")
            self._send(200, {"ok": True, "month": st.get("month"), "rx_bytes": 0, "tx_bytes": 0})
        elif path == "/block":
            body = self._read_json()
            ok, msg = do_block(self.server.state, self.server.conf,
                               str(body.get("reason") or "manual"))
            self._send(200 if ok else 500, {"ok": ok, "msg": msg,
                                            "blocked": bool(self.server.state.snapshot().get("blocked"))})
        elif path == "/unblock":
            ok, msg = do_unblock(self.server.state, "api")
            self._send(200 if ok else 500, {"ok": ok, "msg": msg,
                                            "blocked": bool(self.server.state.snapshot().get("blocked"))})
        else:
            self._send(404, {"ok": False, "error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, state, conf, no_auth, token):
        self.state = state
        self.conf = conf
        self.no_auth = no_auth
        self.token = token
        self._stop = threading.Event()
        ThreadingHTTPServer.__init__(self, addr, Handler)

    def public_conf(self):
        out = dict(self.conf)
        out.pop("token", None)
        return out

    def apply_conf(self):
        pass

    def serve_forever_bg(self):
        t = threading.Thread(target=self.serve_forever, name="http", daemon=True)
        t.start()
        return t


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def is_loopback(host):
    return host in ("127.0.0.1", "::1", "localhost")


def main(argv=None):
    ap = argparse.ArgumentParser(prog=PROG, description="母机网卡流量监控本地服务")
    ap.add_argument("--bind", default=None, help="监听地址，如 127.0.0.1:8899 或 0.0.0.0:8899")
    ap.add_argument("--token", default=None, help="Bearer token（非回环监听时必需）")
    ap.add_argument("--token-from-agent", metavar="AGENT_TOKEN",
                    help="由平台 agent_token 派生 token（HMAC-SHA256(agent_token,'netmon-v1')）")
    ap.add_argument("--interval", type=int, default=None, help="采样间隔秒，默认 60")
    ap.add_argument("--limit-gb", type=float, default=None, help="当月流量上限 GB，0=不限")
    ap.add_argument("--init", action="store_true", help="初始化状态（快照=当前，当月清零）")
    ap.add_argument("--reset", action="store_true", help="清零当月累计")
    ap.add_argument("--once", action="store_true", help="采样一次并打印结果后退出")
    ap.add_argument("--print-token", action="store_true",
                    help="打印由 --token-from-agent 派生的 token 后退出")
    ap.add_argument("--block", metavar="REASON", nargs="?", const="manual", default=None,
                    help="立即断网（危险！会保留 --ssh-ports 指定的端口）")
    ap.add_argument("--unblock", action="store_true", help="立即解除断网")
    ap.add_argument("--limit-dir", choices=("sum", "rx", "tx"), default=None,
                    help="计费方向：sum=上下行合计(默认) / tx=仅出站 / rx=仅入站")
    ap.add_argument("--auto-block", choices=("0", "1"), default=None,
                    help="1=超限自动断网")
    ap.add_argument("--ssh-ports", default=None,
                    help="断网时保留的端口，逗号分隔，默认 22")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.print_token:
        if not args.token_from_agent:
            print("需要 --token-from-agent <agent_token>")
            return 2
        print(derive_token(args.token_from_agent))
        return 0

    ensure_dir(STATE_DIR)
    ensure_dir(CONF_DIR)
    conf = load_json(CONF_FILE, DEFAULT_CONF)

    if args.bind:
        conf["bind"] = args.bind
    if args.interval is not None:
        conf["interval"] = args.interval
    if args.limit_gb is not None:
        conf["limit_gb"] = args.limit_gb
    if args.token:
        conf["token"] = args.token
    elif args.token_from_agent:
        conf["token"] = derive_token(args.token_from_agent)
    if conf.get("interval") is None:
        conf["interval"] = 60
    conf["interval"] = max(5, min(3600, int(conf["interval"])))

    if args.limit_dir:
        conf["limit_dir"] = args.limit_dir
    if args.auto_block is not None:
        conf["auto_block"] = (args.auto_block == "1")
    if args.ssh_ports:
        conf["ssh_ports"] = ",".join(parse_ports(args.ssh_ports))

    state = State()

    if args.unblock:
        ok, msg = do_unblock(state, "cli")
        print(("[OK] " if ok else "[失败] ") + msg)
        return 0 if ok else 1
    if args.block:
        save_json(CONF_FILE, conf)
        ok, msg = do_block(state, conf, args.block)
        print(("[OK] " if ok else "[失败] ") + msg)
        return 0 if ok else 1

    if args.init or args.reset:
        st = state.init()
        print("已初始化: month=%s last_rx=%d last_tx=%d" %
              (st["month"], st["last_rx"], st["last_tx"]))
        return 0

    if args.once:
        st, ifaces = state.sample()
        print(json.dumps(make_status(st, ifaces, conf), ensure_ascii=False, indent=2))
        return 0

    bind = conf.get("bind") or "127.0.0.1:8899"
    host, _, port_s = bind.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        port = int(port_s)
    except ValueError:
        print("非法 bind: %s" % bind, file=sys.stderr)
        return 2

    no_auth = is_loopback(host)
    token = conf.get("token") or ""
    if not no_auth and not token:
        print("拒绝启动：监听 %s 是非回环地址，必须提供 token。\n"
              "  用 --token <值> 或 --token-from-agent <agent_token>（推荐，面板可现算同一值）。\n"
              "  或者改成 --bind 127.0.0.1:8899 并用 SSH 隧道访问。" % host,
              file=sys.stderr)
        return 2

    save_json(CONF_FILE, conf)

    # 启动自检：状态文件里的 blocked 必须和防火墙实际情况一致，否则以防火墙为准
    real_blocked = fw_active()
    st0 = state.snapshot()
    if bool(st0.get("blocked")) != real_blocked:
        log.warning("状态与防火墙不一致：state.blocked=%s 实际=%s，以实际为准",
                    bool(st0.get("blocked")), real_blocked)
        st0["blocked"] = real_blocked
        if not real_blocked:
            st0["blocked_at"] = 0
            st0["block_reason"] = ""
            st0["block_expire"] = 0
        state.save(st0)

    srv = Server((host, port), state, conf, no_auth, token)

    def shutdown(signum, frame):
        log.info("收到信号 %s，退出", signum)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    srv.serve_forever_bg()

    # TTL 看门狗：断网带 block_ttl 时，到点自动解除（防止误封导致永久失联）
    # 解除后会把 auto_block 关掉，否则下一轮采样立刻又封上，变成封-解-封死循环。
    def ttl_watchdog():
        while True:
            time.sleep(10)
            try:
                s = state.snapshot()
                exp = int(s.get("block_expire") or 0)
                if exp and s.get("blocked") and time.time() >= exp:
                    log.warning("断网已达 TTL（%ss），自动解除", conf.get("block_ttl"))
                    do_unblock(state, "ttl")
                    if conf.get("auto_block"):
                        conf["auto_block"] = False
                        save_json(CONF_FILE, conf)
                        log.warning("已同时关闭 auto_block，需要手动重新开启，否则会反复封禁")
            except Exception as e:                       # noqa: BLE001
                log.warning("TTL 看门狗异常: %s", e)

    threading.Thread(target=ttl_watchdog, name="ttl", daemon=True).start()

    log.info("%s %s 已启动  监听 %s:%d  %s  采样间隔 %ss  上限 %s(%s)  自动断网 %s",
             PROG, VERSION, host, port,
             "无鉴权(回环)" if no_auth else "Bearer 鉴权", conf["interval"],
             ("%s" % human_bytes(limit_bytes_of(conf))) if conf.get("limit_gb") else "不限",
             conf.get("limit_dir", "sum"),
             ("开(保留端口 %s)" % conf.get("ssh_ports")) if conf.get("auto_block") else "关")

    # 主循环：内部定时采样，不依赖 cron
    try:
        while True:
            try:
                st, ifaces = state.sample()
                log.debug("采样 month=%s rx=%s tx=%s ifaces=%d",
                          st["month"], human_bytes(st["rx"]), human_bytes(st["tx"]), len(ifaces))
                evaluate_block(state, st, conf)
            except Exception as e:                       # noqa: BLE001
                log.warning("采样失败: %s", e)
            time.sleep(conf["interval"])
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        log.info("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
