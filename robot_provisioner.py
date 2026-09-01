#!/usr/bin/env python3
"""Temporary Wi-Fi AP provisioning for LubanCat robots.

The service is deliberately independent of ROS/ROS2.  NetworkManager owns the
wireless interface; this process only creates a temporary shared hotspot when
the robot has been offline for a while and accepts a new STA profile over HTTP.
"""

from __future__ import annotations

import argparse
import asyncio
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

try:
    from dbus_next import BusType, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, dbus_property, method
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False


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
        self.ble_notify = None

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

    def notify_ble(self) -> None:
        callback = self.ble_notify
        if callback:
            try:
                callback()
            except Exception:
                LOG.debug("BLE status notification failed", exc_info=True)

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
            self.notify_ble()
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
        self.notify_ble()

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
            self.notify_ble()
            LOG.info("Wi-Fi provisioned successfully on %s", ssid)
        except Exception as exc:
            LOG.warning("Wi-Fi provisioning failed: %s", exc)
            if old_connection:
                run_nmcli(["connection", "up", old_connection], timeout=30)
            with self.lock:
                self.last_error = str(exc)
                self.transition = False
            self.notify_ble()
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
                if time.monotonic() - self.last_scan >= 30 and not self.transition:
                    try:
                        self.networks = scan_wifi(self.iface)
                        self.last_scan = time.monotonic()
                    except Exception as exc:
                        LOG.warning("Periodic Wi-Fi scan failed: %s", exc)
                continue
            if self.offline_since is None:
                self.offline_since = time.monotonic()
            if not self.ap_active and not self.transition and time.monotonic() - self.offline_since >= OFFLINE_GRACE_SECONDS:
                self.networks = scan_wifi(self.iface)
                self.last_scan = time.monotonic()
                self.start_ap()

    def serve(self) -> None:
        self.worker = threading.Thread(target=self.monitor, daemon=True)
        self.worker.start()
        if DBUS_AVAILABLE:
            threading.Thread(target=lambda: asyncio.run(BLEProvisioner(self).run()), daemon=True).start()
        while not self.stop_event.wait(1):
            if self.httpd is None:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
                self.httpd.provisioner = self  # type: ignore[attr-defined]
                threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
                LOG.info("Web portal listening on port %s (STA or AP)", HTTP_PORT)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


BLE_SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
BLE_SSID_UUID = "12345678-1234-5678-1234-56789abcdef1"
BLE_PASSWORD_UUID = "12345678-1234-5678-1234-56789abcdef2"
BLE_STATUS_UUID = "12345678-1234-5678-1234-56789abcdef3"
BLE_COMMAND_UUID = "12345678-1234-5678-1234-56789abcdef4"
BLE_WIFILIST_UUID = "12345678-1234-5678-1234-56789abcdef5"


class BLEProvisioner:
    """Small BlueZ GATT bridge using the same Provisioner instance as HTTP."""

    def __init__(self, provisioner: Provisioner) -> None:
        self.p = provisioner
        self.bus = None
        self.status_char = None
        self.ssid = ""
        self.password = ""

    async def run(self) -> None:
        if not DBUS_AVAILABLE:
            LOG.warning("dbus-next unavailable; BLE disabled")
            return
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        app = BLEApplication(self)
        service = BLEService()
        self.status_char = BLECharacteristic(self, BLE_STATUS_UUID, ["read", "notify"], "status")
        ssid_char = BLECharacteristic(self, BLE_SSID_UUID, ["write", "write-without-response"], "ssid")
        password_char = BLECharacteristic(self, BLE_PASSWORD_UUID, ["write", "write-without-response"], "password")
        command_char = BLECharacteristic(self, BLE_COMMAND_UUID, ["write", "write-without-response"], "command")
        list_char = BLECharacteristic(self, BLE_WIFILIST_UUID, ["read"], "list")
        objects = [("/service0", service), ("/char0", ssid_char), ("/char1", password_char), ("/char2", self.status_char), ("/char3", command_char), ("/char4", list_char)]
        self._objects = objects
        self.bus.export("/org/bluez/robotprovisioning", app)
        for suffix, obj in objects:
            self.bus.export("/org/bluez/robotprovisioning" + suffix, obj)
        self.p.ble_notify = self.notify_status
        introspection = await self.bus.introspect("org.bluez", "/org/bluez/hci0")
        adapter = self.bus.get_proxy_object("org.bluez", "/org/bluez/hci0", introspection)
        gatt = adapter.get_interface("org.bluez.GattManager1")
        await gatt.call_register_application("/org/bluez/robotprovisioning", {})
        adv = BLEAdvertisement(self.p.ap_ssid())
        self.bus.export("/org/bluez/robotprovisioning/advertisement0", adv)
        advertising = adapter.get_interface("org.bluez.LEAdvertisingManager1")
        await advertising.call_register_advertisement("/org/bluez/robotprovisioning/advertisement0", {})
        LOG.info("BLE Wi-Fi provisioning ready: %s", self.p.ap_ssid())
        while not self.p.stop_event.is_set():
            await asyncio.sleep(1)

    def read(self, kind: str) -> bytes:
        if kind == "status":
            return json.dumps(self.p.status(), ensure_ascii=False).encode()
        if kind == "list":
            return json.dumps(self.p.networks, ensure_ascii=False).encode()
        return getattr(self, kind).encode()

    def write(self, kind: str, value: bytes) -> None:
        text = value.decode("utf-8", errors="strict").rstrip("\x00")
        if kind in ("ssid", "password"):
            setattr(self, kind, text)
        elif kind == "command":
            cmd = text.strip().upper()
            if cmd == "CONNECT" and valid_text(self.ssid, 32) and len(self.password) <= 128:
                threading.Thread(target=self.p.provision, args=(self.ssid, self.password), daemon=True).start()
            elif cmd == "SCAN" and self.p.iface:
                threading.Thread(target=self._scan, daemon=True).start()
            elif cmd == "RESET":
                self.ssid = ""
                self.password = ""
        self.notify_status()

    def _scan(self) -> None:
        try:
            self.p.networks = scan_wifi(self.p.iface)
            self.p.last_scan = time.monotonic()
            self.notify_status()
        except Exception:
            LOG.exception("BLE Wi-Fi scan failed")

    def notify_status(self) -> None:
        if not self.status_char or not self.bus:
            return
        try:
            self.bus.emit_signal(None, "/org/bluez/robotprovisioning/char2", "org.freedesktop.DBus.Properties", "PropertiesChanged", "sa{sv}as", ["org.bluez.GattCharacteristic1", {"Value": Variant("ay", list(self.read("status")))}, []])
        except Exception:
            LOG.debug("BLE notification unavailable", exc_info=True)


class BLEApplication(ServiceInterface):
    def __init__(self, bridge: BLEProvisioner) -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self.bridge = bridge

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        base = "/org/bluez/robotprovisioning"
        result = {base + "/service0": {"org.bluez.GattService1": {"UUID": Variant("s", BLE_SERVICE_UUID), "Primary": Variant("b", True)}}}
        definitions = [("char0", BLE_SSID_UUID, ["write", "write-without-response"]), ("char1", BLE_PASSWORD_UUID, ["write", "write-without-response"]), ("char2", BLE_STATUS_UUID, ["read", "notify"]), ("char3", BLE_COMMAND_UUID, ["write", "write-without-response"]), ("char4", BLE_WIFILIST_UUID, ["read"])]
        for name, uuid, flags in definitions:
            result[base + "/" + name] = {"org.bluez.GattCharacteristic1": {"UUID": Variant("s", uuid), "Service": Variant("o", base + "/service0"), "Flags": Variant("as", flags)}}
        return result


class BLEService(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.bluez.GattService1")

    @dbus_property()
    def UUID(self) -> "s": return BLE_SERVICE_UUID
    @dbus_property()
    def Primary(self) -> "b": return True


class BLECharacteristic(ServiceInterface):
    def __init__(self, bridge: BLEProvisioner, uuid: str, flags: List[str], kind: str) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self.bridge, self.uuid, self.flags, self.kind = bridge, uuid, flags, kind

    @dbus_property()
    def UUID(self) -> "s": return self.uuid
    @dbus_property()
    def Service(self) -> "o": return "/org/bluez/robotprovisioning/service0"
    @dbus_property()
    def Flags(self) -> "as": return self.flags
    @method()
    def ReadValue(self, options: "a{sv}") -> "ay": return list(self.bridge.read(self.kind))
    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> None: self.bridge.write(self.kind, bytes(value))
    @method()
    def StartNotify(self) -> None: return None
    @method()
    def StopNotify(self) -> None: return None


class BLEAdvertisement(ServiceInterface):
    def __init__(self, name: str) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self.name = name
    @dbus_property()
    def Type(self) -> "s": return "peripheral"
    @dbus_property()
    def ServiceUUIDs(self) -> "as": return [BLE_SERVICE_UUID]
    @dbus_property()
    def LocalName(self) -> "s": return self.name


HTML = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>机器人网络设置</title><style>
:root{color-scheme:light;--blue:#2563eb;--bg:#f3f6fb;--card:#fff;--text:#172033;--muted:#64748b;--line:#e2e8f0}*{box-sizing:border-box}body{margin:0;background:var(--bg);font:15px system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text)}main{max-width:620px;margin:0 auto;padding:24px 16px 48px}.hero{background:linear-gradient(135deg,#1d4ed8,#2563eb 55%,#38bdf8);color:#fff;border-radius:22px;padding:24px;box-shadow:0 12px 30px #1d4ed833}.hero h1{margin:0 0 8px;font-size:25px}.hero p{margin:0;opacity:.88}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;margin-top:16px;padding:20px;box-shadow:0 4px 16px #0f172a0b}.row{display:flex;justify-content:space-between;gap:12px;align-items:center}.pill{border-radius:999px;padding:5px 10px;font-size:12px;background:#dcfce7;color:#166534}.pill.ap{background:#fef3c7;color:#92400e}.label{display:block;color:var(--muted);font-size:13px;margin:16px 0 7px}select,input,button{width:100%;border:1px solid var(--line);border-radius:11px;padding:12px;font:inherit;background:#fff}select:focus,input:focus{outline:3px solid #2563eb22;border-color:var(--blue)}button{border:0;background:var(--blue);color:#fff;font-weight:650;cursor:pointer;margin-top:18px}button.secondary{background:#eff6ff;color:#1d4ed8;margin-top:10px}.password{display:flex;gap:8px}.password input{flex:1}.password button{width:auto;margin:0;padding:0 14px;background:#eef2ff;color:#3730a3}.hint{color:var(--muted);font-size:13px;line-height:1.55}.network{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--line)}.network:last-child{border:0}.signal{color:var(--muted);font-size:13px}#message{margin-top:14px;padding:11px;border-radius:10px;display:none;line-height:1.5}.ok{display:block!important;background:#ecfdf5;color:#166534}.err{display:block!important;background:#fef2f2;color:#991b1b}.spin{display:inline-block;width:14px;height:14px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:6px}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:420px){main{padding:12px 10px 32px}.hero{border-radius:16px;padding:20px}.card{padding:16px}}
</style></head><body><main><section class='hero'><h1>机器人网络设置</h1><p>浏览器与 BLE 共用同一套网络配置服务</p></section><section class='card'><div class='row'><strong>当前状态</strong><span id='state' class='pill'>读取中</span></div><p id='detail' class='hint'>正在读取机器人状态…</p></section><section class='card'><div class='row'><strong>选择目标 Wi‑Fi</strong><button class='secondary' style='width:auto;margin:0' onclick='load(true)'>刷新列表</button></div><label class='label'>附近网络</label><select id='list'><option>正在扫描…</option></select><label class='label'>SSID（隐藏网络可手动填写）</label><input id='ssid' maxlength='32' placeholder='输入 Wi‑Fi 名称'><label class='label'>密码</label><div class='password'><input id='password' type='password' maxlength='128' placeholder='开放网络可留空'><button type='button' onclick='togglePassword()'>显示</button></div><button id='submit' onclick='submitWifi()'>连接此 Wi‑Fi</button><div id='message'></div></section><section class='card'><strong>使用提示</strong><p class='hint'>连接过程中当前网络会短暂断开，但机器人上的 ROS/ROS2 进程不会停止。连接成功后，请让手机回到目标 Wi‑Fi，再使用新的机器人 IP 访问本页面。</p></section></main><script>
let timer;function show(text,ok){let e=document.getElementById('message');e.textContent=text;e.className=ok?'ok':'err'}function togglePassword(){let e=document.getElementById('password'),b=document.querySelector('.password button');e.type=e.type==='password'?'text':'password';b.textContent=e.type==='password'?'显示':'隐藏'}function render(s){let st=document.getElementById('state'),d=document.getElementById('detail');st.textContent=s.transition?'正在切换':s.ap_active?'临时热点':'已联网';st.className='pill '+(s.ap_active?'ap':'');d.textContent=(s.sta_connection?'当前网络：'+s.sta_connection+'　':'')+(s.ip?'IP：'+s.ip:'')+(s.ap_active?'　热点：'+s.ap_ssid:'');let l=document.getElementById('list');if(document.activeElement!==l){l.innerHTML='';(s.networks||[]).forEach(n=>{let o=document.createElement('option');o.value=n.ssid;o.textContent=n.ssid+'　信号 '+n.signal+'%　'+(n.security||'开放');l.appendChild(o)});if(!s.networks?.length){l.innerHTML='<option>未发现网络，请手动填写 SSID</option>'}}l.onchange=()=>document.getElementById('ssid').value=l.value}async function load(force){try{let r=await fetch('/api/status?x='+Date.now());let s=await r.json();render(s);if(force)show('Wi‑Fi 列表已刷新',true)}catch(e){show('无法读取机器人状态，请确认手机仍在同一网络。',false)}}async function submitWifi(){let ssid=document.getElementById('ssid').value||document.getElementById('list').value,p=document.getElementById('password').value;if(!ssid){show('请先选择或填写 SSID。',false);return}let b=document.getElementById('submit');b.disabled=true;b.innerHTML='<span class="spin"></span>正在连接…';try{let r=await fetch('/api/provision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password:p})});let x=await r.json();show(x.message||x.error||'配置已提交',r.ok);if(r.ok)timer=setInterval(()=>load(false),3000)}catch(e){show('网络切换已开始，页面即将断开。',true)}finally{setTimeout(()=>{b.disabled=false;b.textContent='连接此 Wi‑Fi'},5000)}}load(false);</script></body></html>"""


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
