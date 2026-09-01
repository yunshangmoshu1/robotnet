# Robot Network Provisioner

适用于 LubanCat / Ubuntu 20.04 / NetworkManager 的单无线网卡配网服务。

## 行为

- 机器人在线时，手机与机器人处于同一局域网，可直接访问 `http://机器人的当前IP:8080/` 修改网络。
- 在线状态下每 30 秒后台扫描并更新缓存；进入临时热点前会再扫描一次。
- 临时热点开启后不再扫描，避免单无线网卡导致热点中断；页面显示上次扫描时间并允许手动填写 SSID。
- wlan0 无有效连接时不自动创建热点，离线配网统一使用 BLE，避免单无线网卡 AP 抢占扫描能力。
- 热点名称为 `LubanCat-<无线网卡 MAC 后六位>`。
- 热点密码为 `Robot-<MAC 后六位>`（建议量产时打印在设备标签上）。
- 旧 AP 入口仅保留为手动兼容功能，不作为默认流程。
- 页面显示机器人扫描到的 Wi-Fi 列表，也允许手动输入 SSID。
- 提交后关闭 AP，使用 NetworkManager 连接目标 Wi-Fi。
- 连接失败时尝试恢复旧连接，然后重新开启 AP。
- 不停止 ROS/ROS2 进程，不依赖 ROS。
- 每台机器人首次运行时生成并持久化独立的 `robot_id`、热点 SSID 和热点密码，保存在 `/var/lib/robot-network-provisioner/device.json`。
- 浏览器可按机器人 ID 在本机保存已输入的 Wi-Fi 密码，第二次访问同一机器人时自动填充；NetworkManager 同时保留机器人已成功连接的网络配置。

BLE 已融合到本服务中，UUID 与原版本兼容。使用 nRF Connect 或现有 Flutter App 连接 `LubanCat-XXXX` 后，向 `...abcdef4` 写入 `PROVISION`（或 `OPEN_BROWSER`），机器人会开启临时热点；随后手机连接热点并用浏览器访问 `http://192.168.4.1:8080/`。BLE 不再传输 Wi‑Fi 密码，最终配置全部通过浏览器完成。读取/订阅 `...abcdef3` 获取状态，向 `...abcdef4` 写入 `SCAN` 可刷新列表。

部署融合版本前应停用旧的 `ble-provisioning.service`，避免两个 GATT 服务重复注册同一组 UUID。

## 安装

```bash
sudo install -d /opt/robot-network-provisioner
sudo install -m 0755 robot_provisioner.py /opt/robot-network-provisioner/
sudo install -m 0644 robot-network-provisioner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robot-network-provisioner.service
```

## 注意

离线 BLE 配网网页位于 `web/index.html`，通过 Web Bluetooth 连接机器人后直接发送 SSID/密码；该页面必须从 HTTPS 安全来源打开。机器人端不再需要开启 AP。需要注意 Web Bluetooth 不是所有浏览器都支持，使用前应在目标 Android 浏览器上验证。

在线网页仍使用：`http://机器人IP:8080/`。离线 BLE 页面流程：打开 HTTPS 页面 → 点击“连接机器人蓝牙” → 选择 Wi-Fi → 输入密码 → 连接。

生产使用前应增加物理/显示屏触发的配网窗口，并对网页增加一次性配网 PIN；当前自动 AP 模式适合现场验证。
