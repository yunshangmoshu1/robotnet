#!/usr/bin/env python3
"""Temporary Wi-Fi AP provisioning for LubanCat robots.

The service is deliberately independent of ROS/ROS2.  NetworkManager owns the
wireless interface; this process only creates a temporary shared hotspot when
the robot has been offline for a while and accepts a new STA profile over HTTP.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


LOG = logging.getLogger("robot-provisioner")
STATE_DIR = Path("/var/lib/robot-network-provisioner")
STATE_FILE = STATE_DIR / "state.json"
AP_PROFILE = "robot-provisioning-ap"
AP_ADDRESS = "192.168.4.1/24"
AP_URL = "http://192.168.4.1/"
HTTP_PORT = 80
OFFLINE_GRACE_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 35


def run_nmcli(args: List[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nmcli", *args], capture_output=True, text=True, timeout=timeout
    )


def split_nmcli_line(line: str) -> List[str]:
    """Split nmcli terse output, honoring backslash-escaped separators."""
    fields: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def wifi_interface() -> Optional[str]:
    result = run_nmcli(["-t", "-f", "DEVICE,TYPE", "device"], timeout=5)
    for line in result.stdout.splitlines():
        fields = split_nmcli_line(line)
        if len(fields) >= 2 and fields[1] == "wifi" and fields[0]:
            return fields[0]
    return None


def active_wifi(iface: str, include_ap: bool = False) -> Optional[str]:
    result = run_nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], timeout=5)
    for line in result.stdout.splitlines():
        fields = split_nmcli_line(line)
        if len(fields) >= 4 and fields[0] == iface and fields[1] == "wifi":
            if fields[2] == "connected" and (include_ap or fields[3] != AP_PROFILE):
                return fields[3]
    return None


def ip_address(iface: str) -> Optional[str]:
    result = run_nmcli(["-t", "-f", "IP4.ADDRESS", "device", "show", iface], timeout=5)
    for line in result.stdout.splitlines():
        if line.startswith("IP4.ADDRESS") and ":" in line:
            return line.split(":", 1)[1].split("/", 1)[0]
    return None


def scan_wifi(iface: str) -> List[Dict[str, Any]]:
    # Scan before AP creation.  Scanning while the AP is live can interrupt a
    # single-radio hotspot on some rtw88 driver versions.
    run_nmcli(["device", "wifi", "rescan", "ifname", iface], timeout=15)
    time.sleep(2)
    result = run_nmcli(
        ["-t", "--escape", "yes", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", iface],
        timeout=20,
    )
    networks: Dict[str, Dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        fields = split_nmcli_line(line)
        if len(fields) < 3 or not fields[0]:
            continue
        ssid = fields[0]
        try:
            signal_value = int(fields[1])
        except ValueError:
            signal_value = 0
        networks.setdefault(ssid, {"ssid": ssid, "signal": signal_value, "security": fields[2]})
        if signal_value > networks[ssid]["signal"]:
            networks[ssid]["signal"] = signal_value
    return sorted(networks.values(), key=lambda item: item["signal"], reverse=True)


def valid_text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and "\x00" not in value


class Provisioner:
    def __init__(self) -> None:
        self.iface = wifi_interface()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ap_active = False
        self.transition = False
        self.last_error = ""
        self.networks: List[Dict[str, Any]] = []
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.worker: Optional[threading.Thread] = None
        self.offline_since: Optional[float] = None
        self.previous_connection: Optional[str] = None
        self.last_scan = 0.0

    def status(self) -> Dict[str, Any]:
        # Populate the list on the first browser visit while the STA is online.
        if self.iface and not self.networks and time.monotonic() - self.last_scan > 30:
            try:
                self.networks = scan_wifi(self.iface)
                self.last_scan = time.monotonic()
            except Exception as exc:
                LOG.warning("Wi-Fi scan failed: %s", exc)
        with self.lock:
            return {
                "ap_active": self.ap_active,
                "ap_ssid": self.ap_ssid(),
                "ap_url": AP_URL if self.ap_active else "",
                "online_url": f"http://{ip_address(self.iface)}/" if self.iface and ip_address(self.iface) else "",
                "wifi_interface": self.iface,
                "sta_connection": active_wifi(self.iface) if self.iface else None,
                "ip": ip_address(self.iface) if self.iface else None,
                "transition": self.transition,
                "error": self.last_error,
                "networks": self.networks,
            }

    def ap_ssid(self) -> str:
        # Stable, unique enough for a local provisioning hotspot.
        mac = "robot"
        if self.iface:
            result = subprocess.run(["cat", f"/sys/class/net/{self.iface}/address"], capture_output=True, text=True)
            mac = result.stdout.strip().replace(":", "")[-6:] or mac
        return f"LubanCat-{mac.upper()}"

    def ap_password(self) -> str:
        # The password is deterministic so it can be printed on the robot label
        # or displayed by the existing UI without needing another service.
        return f"Robot-{self.ap_ssid().split('-', 1)[-1]}"

    def start_ap(self) -> bool:
        if not self.iface:
            self.last_error = "No Wi-Fi interface found"
            return False
        with self.lock:
            if self.ap_active:
                return True
            self.previous_connection = active_wifi(self.iface)
            self.transition = True
        try:
            run_nmcli(["device", "disconnect", self.iface], timeout=15)
            run_nmcli(["connection", "delete", AP_PROFILE], timeout=10)
            result = run_nmcli(
                [
                    "device", "wifi", "hotspot", "ifname", self.iface,
                    "con-name", AP_PROFILE, "ssid", self.ap_ssid(),
                    "password", self.ap_password(),
                ],
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Unable to create hotspot")
            run_nmcli(["connection", "modify", AP_PROFILE, "ipv4.method", "shared", "ipv4.addresses", AP_ADDRESS, "ipv6.method", "disabled", "connection.autoconnect", "no"], timeout=10)
            up = run_nmcli(["connection", "up", AP_PROFILE], timeout=30)
            if up.returncode != 0:
                raise RuntimeError(up.stderr.strip() or "Unable to activate hotspot")
            with self.lock:
                self.ap_active = True
                self.transition = False
                self.last_error = ""
            LOG.info("Provisioning AP active: %s / %s", self.ap_ssid(), self.ap_password())
            return True
        except Exception as exc:
            with self.lock:
                self.transition = False
                self.last_error = str(exc)
            LOG.exception("Failed to start AP")
            return False

    def stop_ap(self) -> None:
        with self.lock:
            if not self.ap_active:
                return
            self.transition = True
        run_nmcli(["connection", "down", AP_PROFILE], timeout=20)
        with self.lock:
            self.ap_active = False
            self.transition = False

    def provision(self, ssid: str, password: str) -> None:
        with self.lock:
            self.transition = True
            self.last_error = ""
        old_connection = self.previous_connection or (active_wifi(self.iface) if self.iface else None)
        try:
            # Stop AP before using the single radio as STA.
            self.stop_ap()
            run_nmcli(["connection", "delete", "robot-provision-candidate"], timeout=10)
            args = ["device", "wifi", "connect", ssid, "ifname", self.iface, "name", "robot-provision-candidate"]
            if password:
                args.extend(["password", password])
            result = run_nmcli(args, timeout=CONNECT_TIMEOUT_SECONDS)
            if result.returncode != 0 or not active_wifi(self.iface):
                raise RuntimeError(result.stderr.strip() or "Wi-Fi connection failed")
            with self.lock:
                self.transition = False
                self.last_error = ""
            LOG.info("Wi-Fi provisioned successfully on %s", ssid)
        except Exception as exc:
            LOG.warning("Wi-Fi provisioning failed: %s", exc)
            if old_connection:
                run_nmcli(["connection", "up", old_connection], timeout=30)
            with self.lock:
                self.last_error = str(exc)
                self.transition = False
            self.networks = scan_wifi(self.iface) if self.iface else []
            self.start_ap()

    def monitor(self) -> None:
        while not self.stop_event.wait(5):
            if not self.iface:
                self.iface = wifi_interface()
                continue
            if active_wifi(self.iface):
                self.offline_since = None
                if self.ap_active:
                    self.stop_ap()
                continue
            if self.offline_since is None:
                self.offline_since = time.monotonic()
            if not self.ap_active and not self.transition and time.monotonic() - self.offline_since >= OFFLINE_GRACE_SECONDS:
                self.networks = scan_wifi(self.iface)
                self.start_ap()

    def serve(self) -> None:
        self.worker = threading.Thread(target=self.monitor, daemon=True)
        self.worker.start()
        while not self.stop_event.wait(1):
            if self.httpd is None:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
                self.httpd.provisioner = self  # type: ignore[attr-defined]
                threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
                LOG.info("Web portal listening on port %s (STA or AP)", HTTP_PORT)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


HTML = """<!doctype html><html lang='zh-CN'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>机器人 Wi‑Fi 配网</title><style>body{font:16px sans-serif;max-width:560px;margin:2em auto;padding:0 1em}label{display:block;margin-top:1em}select,input,button{width:100%;padding:.7em;font-size:1em}button{margin-top:1.4em}#msg{margin-top:1em;white-space:pre-wrap}</style>
<h2>机器人 Wi‑Fi 配网</h2><p>请选择目标 Wi‑Fi 并输入密码。提交后热点会暂时断开，机器人将连接目标网络。</p>
<label>Wi‑Fi<select id='ssid'><option>正在扫描…</option></select></label>
<label>SSID（也可手动输入）<input id='ssidText' maxlength='32' placeholder='隐藏网络请手动输入'></label>
<label>密码（开放网络可留空）<input id='password' type='password' maxlength='128'></label>
<button onclick='submitWifi()'>连接</button><div id='msg'></div>
<script>
async function load(){let r=await fetch('/api/status');let s=await r.json();let e=document.getElementById('ssid');e.innerHTML='';(s.networks||[]).forEach(n=>{let o=document.createElement('option');o.value=n.ssid;o.textContent=n.ssid+' ('+n.signal+'%) '+(n.security||'开放');e.appendChild(o)});e.onchange=()=>document.getElementById('ssidText').value=e.value;}
async function submitWifi(){let ssid=document.getElementById('ssidText').value||document.getElementById('ssid').value;let password=document.getElementById('password').value;document.getElementById('msg').textContent='正在连接，请等待…';let r=await fetch('/api/provision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password})});document.getElementById('msg').textContent=await r.text();} load();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotProvisioner/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("HTTP %s", fmt % args)

    @property
    def provisioner(self) -> Provisioner:
        return self.server.provisioner  # type: ignore[attr-defined]

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = HTML.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/status":
            self.send_json(self.provisioner.status())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/provision":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 4096)))
            ssid = payload.get("ssid")
            password = payload.get("password", "")
            if not valid_text(ssid, 32) or not isinstance(password, str) or len(password) > 128:
                self.send_json({"ok": False, "error": "SSID 或密码格式无效"}, 400)
                return
            self.send_json({"ok": True, "message": "配置已接收，热点将断开，请等待机器人连接新网络。"})
            threading.Thread(target=self.provisioner.provision, args=(ssid, password), daemon=True).start()
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one status/scan check and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    provisioner = Provisioner()
    if not provisioner.iface:
        LOG.error("No Wi-Fi interface found")
        return 2
    if args.once:
        print(json.dumps(provisioner.status(), ensure_ascii=False, indent=2))
        return 0
    signal.signal(signal.SIGTERM, lambda *_: provisioner.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: provisioner.stop_event.set())
    provisioner.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
