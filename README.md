# Robot Network Provisioner

适用于 LubanCat / Ubuntu 20.04 / NetworkManager 的单无线网卡配网服务。

## 行为

- 机器人在线时，手机与机器人处于同一局域网，可直接访问 `http://机器人的当前IP/` 修改网络。
- wlan0 无有效连接持续 30 秒后，自动创建临时热点。
- 热点名称为 `LubanCat-<无线网卡 MAC 后六位>`。
- 热点密码为 `Robot-<MAC 后六位>`（建议量产时打印在设备标签上）。
- 手机连接热点后访问 `http://192.168.4.1/`。
- 页面显示机器人扫描到的 Wi-Fi 列表，也允许手动输入 SSID。
- 提交后关闭 AP，使用 NetworkManager 连接目标 Wi-Fi。
- 连接失败时尝试恢复旧连接，然后重新开启 AP。
- 不停止 ROS/ROS2 进程，不依赖 ROS。

机器人上原有的 `ble-provisioning.service` 仍可作为 BLE 入口；浏览器和 BLE 最终都由 NetworkManager 执行 Wi-Fi 切换。BLE 入口适合手机无法加入机器人当前局域网的场景。

## 安装

```bash
sudo install -d /opt/robot-network-provisioner
sudo install -m 0755 robot_provisioner.py /opt/robot-network-provisioner/
sudo install -m 0644 robot-network-provisioner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robot-network-provisioner.service
```

## 注意

这是第一版普通 Wi-Fi 配网实现，暂不包含企业 802.1X、Captive Portal、BLE 配网和运行中改网认证。AP+STA 并发不需要也不使用。

生产使用前应增加物理/显示屏触发的配网窗口，并对网页增加一次性配网 PIN；当前自动 AP 模式适合现场验证。
