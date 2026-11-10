# AirDrop / AWDL 逆向笔记

## 测试环境

| 项目 | 值 |
|---|---|
| 主机 | AMD Ryzen 7 9800X3D / Colorful B850M-PLUS |
| 系统 | macOS 26.7.2 (25G332, Darwin 25) |
| 网卡 | MediaTek MT7925（AMD RZ717，PCI `14c3:0717`） |
| 驱动 | 自编译 MtkWlan（基于 0.12.1 源码 + probe 补丁） |
| **测试手机** | **iPhone 14 Pro，iOS 26.1，国行版** |
| 手机设置 | AirDrop 面板打开、蓝牙开、低电量模式关、屏幕常亮 |

## 已验证的里程碑

### 里程碑 1：监听模式 + 信道控制 ✅
- 命令：`MCU_UNI_CMD_SNIFFER (0x24)` + `UNI_SNIFFER_ENABLE (tag 0)` + `UNI_SNIFFER_CONFIG (tag 1)`
- 证据：6 信道约 30 信标/秒、157 信道约 9 信标/秒
- 代码位置：`mtk-src/src/MtkJoin.cpp` 的 `monitorOn()` / `monitorOff()`

### 里程碑 2：原始帧送达用户态 ✅
- 传输：IOUserClient 环形缓冲（`kMtkUcMonRead = 6`），结构体输入带长度（MACH 限制约 4KB）
- 证据：读到真实信标 `sub=8`、probe response `sub=5`，MAC 合法
- 代码位置：`MtkUserApi.h`（`mtk_mon_batch`）、`MtkUserClient.cpp`、`MtkJoin.cpp` 的 `monCapture()` / `monDrain()`

### 里程碑 3：抓到 AWDL 帧 ✅（2026-11-07 18:32）
- 信道：**149**
- 帧样例：`7f 00 17 f2 08 10 03 00 ...`（category 127 / Apple OUI / OUI type 08 = AWDL）
- 发送方：`a2=6a:c5:f5:5c:ce:04`（iPhone 14 Pro 的随机化 AWDL MAC）
- 目标：`a1=ff:ff:ff:ff:ff:ff`（广播 = AWDL 同步帧特征）

## 工具：mtkmon

```
mtkmon <channel>    停在该信道并开启嗅探（需 root）
mtkmon off          关闭嗅探
mtkmon disconnect   断开连接并关闭自动重连（需 root）
mtkmon read [n]     读取 n 批抓到的帧（每批最多 4 帧 × 512 字节）
```

## 构建 / 安装 / 还原

```bash
cd mtk-src && ./tools/build-mac.sh                 # 编译（CLT 即可，产物可复现）
sudo bash ../swap_driver.sh                        # 装入 ESP（自动备份原版）
sudo bash ../switch_to_eth_mode.sh                 # 去掉 mtkfe=native，可用 disconnect
sudo bash ../restore_driver.sh                     # 还原原版驱动
sudo bash ../restore_native_mode.sh                # 还原原生 Wi-Fi 前端
```

## 关键结论 / 踩过的坑

1. **ROC 在未关联状态下不调谐射频**——必须用 SNIFFER 命令；
2. 驱动原本**没有设置信道的命令**，扫描走固件 hw_scan；
3. IOUserClient 的结构体输出**超过约 4KB 会 MACH_RCV_TOO_LARGE**，所以每批要小；
4. `structureOutputSize` 对 OOL 缓冲上报为 0，长度要用结构体输入自己传；
5. `monCapture` 必须复刻 `rxFrame` 的坏帧检查（FCS/AMSDU/ICV），否则抓到大量垃圾帧；
6. 帧数据要从 `p + rxFrameOffset(rxd1, rxd2)` 开始，不能直接复制缓冲区开头；
7. `MtkMonTotal` 只在 `monDrain` 时更新属性，不读就一直显示旧值（不要据此判断停摆）。

### 里程碑 4：AWDL 帧完整解码 ✅（2026-11-07 18:41）

抓到并解析 iPhone 14 Pro 的 **Master Indication Frame (MIF)**：

```
7f 00 17 f2 | 08 10 03 00 | 81 87 84 6b 3d 87 84 6b | 04 49 00 | 95 19 00 00 00 10 00 6e 00 00 18 ...
```

| 字段 | 值 |
|---|---|
| AF type / version | 0x08 / 0x10(16) |
| subtype | 0x03 = MIF（Master Indication Frame） |
| phy_time - target_time | tx_delay = 68 µs |
| TLV type | 0x04 = Synchronization Parameters（len 73） |
| tx_chan | 0x95 = **149** ✅ 与监听信道一致 |
| tx_counter | 25 |
| aw_period / AF period | 16 / 110 |
| flags | 0x1800 |

解析参考：Wireshark `packet-awdl.c` 的 `dissect_awdl_action()` 与 `awdl_tag_sync_params()`。

## 待办（更新）
- [x] 用 512 字节上限抓完整 AWDL 同步帧，解析 `awdl_af_*` 字段
- [ ] 抓 128+ 字节，解出 master MAC / channel sequence / SSID hash
- [ ] 对接 owly（t4t5/owly）的 radio backend
- [ ] 与原生前端共存时的信道争用问题

### 里程碑 5：AWDL 链路层节点（2026-11-07 19:58）

自写 C 模块 `mtk-src/tools/awdl/`（awdl.h / awdl_frame.c）：
- 按 SPEC §3.2 构造/解析 action frame（TLV 4/5/6/7/12/16/18/21/24）
- BSSID 固定 `00:25:00:ff:94:73`（AWDL-1）
- 只跟随满足 AWDL-17 的 master（MIF + Version TLV）
- 主时钟跟踪 + 时隙边界对齐发送（`down_tu` / `aw_seq`）
- 信道跳变：按 master 信道序列在 6/149/157 间切换

实测：`txOk` 持续增长、`txFail=0`、跳变节奏正确（slot 8→6, 9→157, 10→149, 11→157）。

### 结论：AirDrop 的真正瓶颈

规范 `owly-SPEC.md`（已存工作区，41KB）显示：
- MDNS-1：接收方必须在 **awdl0** 上广播 `_airdrop._tcp`（TXT flags ≥ 0x089）
- NET-2：邻居地址由 MAC 推导，不需要 ND
- TLS-1：TLS 1.2/1.3 + HTTP/1.1 `/Discover` `/Ask` `/Upload`

而我们的驱动**没有 awdl0**，只有以太网形式的 en1。因此：

**下一步（唯一现实路线）**：在驱动里创建虚拟 `awdl0` 网络接口，让 macOS 原生协议栈（IPv6/TCP/TLS/mDNSResponder）承载 AirDrop。

| 阶段 | 内容 | 估计 |
|---|---|---|
| 1 | 驱动虚拟 awdl0（帧注入/封装） | 3–6 周，高风险 |
| 2 | mDNS 服务注册（`_airdrop._tcp`） | 1 周 |
| 3 | TLS/HTTP AirDrop 端点 | 2–3 周 |

替代方案（用户态自写 IPv6+UDP+TCP+TLS 栈）不现实，已排除。

### 里程碑 6：数据面尝试（2026-11-07 20:39）

新增工具与驱动能力：
- 驱动：`kMtkUcTxData`（原始数据帧发送）、A-MSDU 聚合帧捕获
- `awdlbridge`：utun 虚拟接口 ↔ AWDL 数据帧双向桥接，多播按时隙 0/10 调度
- `awdlble`：AirDrop BLE 广播（BLE-1/BLE-2，Apple 厂商数据 0x004C + TLV 0x05），实测广播成功
- `blescan`：BLE 扫描自检

与 iPhone 的 MIF 逐字段对比后修正：
| 字段 | iPhone 实际值 | 我们的修正 |
|---|---|---|
| Version TLV | `0xa0`（版本 10） | 改为 0xa0 |
| 选举度量 | metric=515, dist=0 | 跟随 master（515+1） |
| 数据路径 TLV 12 | 47 字节，flags 0xbf23 | 复用 iPhone 原始字节并替换 MAC |
| 未知 TLV 17 | 14 字节 | 复用 |

**卡点**：即便帧结构与合法帧几乎一致（TLV 4/5/12/17/18/24 全部来自苹果原帧），
iPhone 仍然**不发送任何 AWDL 数据帧**（20 秒 dump 只有 7 个 MIF，0 个数据帧），
ping6 100% 丢包。

结论：手机不把我们视为有效 AWDL 对端，或它的 AirDrop 服务器（mDNS）未启动。
规范 §2 说明接收方需要 BLE 触发才启动服务器——BLE 广播已发出（自扫描验证），但可能因为
零联系人哈希 / Everyone 模式判定 / 系统层合并且未被手机接受。

### 下一步候选（按可行性排序）
1. 检查是否需要**非零联系人哈希**（BLE-2 的 4 个 2 字节 SHA-256 前缀）才能唤醒
2. 驱动里放开 RX 过滤器到**所有数据帧**，验证数据通路本身（当前只收 AWDL LLC/SNAP 帧）
3. 完整移植 owly 的 sync/election/SSD 状态机（以月计）

### 里程碑 7：BLE 触发层验证（2026-11-07 21:01）

- `awdlble`（CoreBluetooth 广播）报告成功，标签固定为 `aa bb cc`
- `blescan` 扫描结果中**没有** `4c 00 05 12 40 aa bb cc`
- 能扫到的只有 Apple 系统广播（`4c 00 0c`、`4c 00 10` 等）

**结论：macOS 的 CoreBluetooth 不允许第三方程序发出自定义厂商数据**，BLE 触发链路走不通。

### 最终状态

| 层 | 状态 |
|---|---|
| 驱动：监听/信道/管理帧+数据帧收发 | ✅ 实测通过 |
| AWDL 帧构造解析（版本 0xa0、选举、同步、信道序列） | ✅ 与苹果原帧一致 |
| 时钟同步、时隙对齐、信道跳变 | ✅ |
| utun 桥接（系统 IPv6 栈） | ✅ |
| **BLE AirDrop 广播** | ❌ **macOS 限制，需外置 BLE** |
| iPhone 的 mDNS 服务器 | ❌ 因缺少 BLE 触发未启动 |

### 后续路径

1. **ESP32-C3 / nRF52840**：发送 22 字节 AirDrop 广播（`4c 00 05 12 40 TT TT TT 00 00 00 00 03 00×8 00`），
   唤醒 iPhone 的 AirDrop 服务器 → 我们的监听立刻能看到 mDNS
2. 之后按 §5/§6 实现 mDNS 应答与 TLS/HTTP 端点
3. 备选：逆向 MtkBt 的 HCI 接口直接发广播（风险高）

## 里程碑 8：awdl0 出现（2026-11-07 23:50，改用 thanhnndev 上游驱动）

**方案切换**：放弃自建 IO80211 骨架（`mtk80211/`，卡在 OCAK vtable 与缺失私有类型），改用
上游驱动 <https://github.com/thanhnndev/mt7925-macos> 的 `dev/awdl` 分支（BSD-3-Clause-Clear）。

构建与安装：
- `bash scripts/build.sh`（只用 CLT，自动拉 MacKernelSDK）→ 4 个 kext
- 需给 `kexts/itlmt7925/Info.plist` 加 `0x071714c3`（我们的 RZ717 是 14c3:0717，上游只写 7925）
- 启动参数：`itlmt_native=1 itlmt_native_if=1 itlmt_native_dp=1 itlmt_fast=6 itlmt_awdl=1`
- 安装脚本：`install_upstream_awdl.sh`，回滚：`restore_upstream_awdl.sh`

**结果**：
```
awdl0: flags=8843 ... inet6 fe80::5c28:faff:fe1c:2847%awdl0
en2: Third-Party Wireless Card | itlmt7925Native 0.1
访达 → 隔空投送：「我的设备」里出现 iPhone，收发选项可选「所有人」
```

**未完成**：
- iPhone 侧看不到 Mac（怀疑 Mac 未在 awdl0 上广播 `_airdrop._tcp`，或数据面未通）
- 原生 Wi-Fi 扫描结果在 25G332 上不返回（ioctl cmd=10 被接受但无结果）

ABI 对比结论：`skywalk-if.diff` 在 25G241 与 25G332 上**完全一致**，ABI 无变化。

### 里程碑 9：蓝牙修复 + 监听窗口（2026-11-08 01:16）

**蓝牙**（原本 `MtkBtState=error`，`half-loaded, subsys reset`，系统显示假芯片 `BCM_4350C2`）：
- 原因：上游 `MtkBtFw` 的 Info.plist 匹配 `idVendor=0x0489 idProduct=0xE11E`（他们测试机的 ID）
- 我们的设备是 **`0x0E8D:0x0717`**（USB 侧），改对后立即绑定：
```
com.itlmt7925.mtkbtfw (0.1.0) 加载
Address: 14:5A:41:0C:B5:10  State: On  Chipset: THIRD_PARTY_DONGLE  FW: v9736 c4881
```
- 使用分支 `fix/bt-download-retry`（含下载重试修复），源码在 `mt7925-btfix/`
- 同时禁用了 `MtkBt.kext`、`SimpleMtkBtFw.kext`

**AWDL 监听窗口**（补丁，`mt7925-upstream/kexts/itlmt7925Native/itlmt7925_awdl.cpp`）：
- 原实现：`awdlLsnArm()` 只在有 peer 时被调用 → 无 peer 永不监听 → rx=0 死锁
- 补丁 1：`awdlLsnArm()` 加无 peer 回退（轮询 6/44/149 开纯监听窗口）
- 补丁 2：调用处去掉 `awdlListenPeer(now)` 条件，PSF 一律在监听窗口里发
- 补丁 3：回退窗口加 70ms 节流（此前 160 窗口/秒，射频无法驻留）
- 结果：`lsn=17210/539/0 ch=149`（窗口正常运行），但 `rx=0` 仍未收到帧

**待查**：监听窗口内核心的 RX 下发路径（帧是否到达、是否被 BSSID/SNAP 过滤丢弃）。

### 里程碑 10：Wi-Fi + 蓝牙 + AWDL 全部工作（2026-11-08 01:43）

**关键突破：`itlmt_m1=0` 启动参数**
- `itlmt7925.cpp` 中 M1（固件初始化线程）只在 `PE_parse_boot_argn("itlmt_m1", ...)` 成功时启动：
  `runM1 = PE_parse_boot_argn("itlmt_m1", &m1, ...) && !m1Started;`
- 没有这个参数 → M1/M2/M3 全不运行 → `wifi-state=off` → 原生扫描恒返回 `kIOReturnNotReady`
- 加上 `itlmt_m1=0` 后：固件初始化完成，Wi-Fi 扫描/关联全部正常

**当前完整状态（2026-11-08 01:43 冷启动）**：
```
蓝牙: 14:5A:41:0C:B5:10 · On · THIRD_PARTY_DONGLE · FW v9736 c4881
Wi-Fi: wifi-state = "connected"（en2 已关联）
awdl0: fe80::9c58:a3ff:fee4:f517
AWDL: peer4=690 rx=690 ptl=110 dtx=36 drx=40 last=6ac5f55cce04（iPhone）
```

**已验证**：
- Mac 能持续收到 iPhone 的 AWDL 帧（rssi -70 dBm）
- AWDL 数据帧双向流动（dtx/drx 均增长）
- Mac 在 awdl0 上广播 `_airdrop._tcp`（实例 51390b876287）
- 访达 AirDrop 可见 iPhone

**未完成**：iPhone 看不到 Mac（与上游 §10.54 "the iPhone's unicast arrives; ours mostly does not" 同一阶段）
可能的下一步：BLE LE 广播（上游有 `fix/bt-le-advertising` 分支）或 mDNS 响应帧的对齐时序。

### 里程碑 11：iPhone 发现 / 发送方向的阻断点（2026-11-08 02:30）

**状态（最新内核 95c9a84 + 我们的补丁）**
```
蓝牙: 14:5A:41:0C:B5:10 · On
Wi-Fi: connected
awdl0: fe80::30e1:45ff:fe27:9d3e
AWDL: peer4=537 rx=537 dtx=54 drx=20
```

**已定位的两个阻断点**

1. **LE 广播不发包（iPhone 发现 Mac 的前提）**
   - iPhone 蓝牙设置里能看到 Mac（经典蓝牙可发现广播正常）
   - nRF Connect 看不到任何 LE 广播
   - CoreBluetooth 报告 `isAdvertising=true`，但空中无信号
   - `leadv2`（修正了方法类型编码：`i28@0:8C16*20`，第二参数是 char* 而非 NSData）返回 0x0，仍无广播
   - 结论：MT7925 固件/驱动不支持 LE 广播（需分析 MtkBtFw 的 WMT 启用序列）

2. **Mac→iPhone 发送停在"等待中"**
   - 访达拖文件到 iPhone：永远等待
   - 与上游 §10.67 "AirDrop HELLO works, the Ask never arrives" 同一阶段
   - 参考文档（QQ 传来的私有 Rust 项目修复档案）：iOS 26/27 在 TLV4 信道表末尾追加 2 字节（如 `20 e4`），解析器若要求填充为 0 会拒绝整帧 → iPhone 不进 peer 表 → 单播全落空
   - thanhnndev 的 C++ 实现已在构造端写 73 字节（含 2 字节零填充），解析端 `awdlTlv4Timeline` 不检查该尾随 → 非此因

**下一步**
- LE 广播：对比 Linux btmtk.c 的 WMT 初始化，检查 MtkBtFw 是否遗漏广播使能
- 单播：需要包级抓包（root）或新增驱动计数器（TX 落在哪个窗口/信道）

### 里程碑 12：LE 能力的最终判定（2026-11-08 02:55）

**LE 扫描：完全正常**
```
CoreBluetooth central scan (10 s): state=5, 找到 27 个设备
  found: iPhone rssi=-66 …
```

**LE 广播：系统认为在发，空中没有**
```
CBPeripheralManager: state=5, startAdvertising → isAdvertising=true（持续 50 秒）
iPhone nRF Connect: 看不到 MtkTest
```

**私有 API 读取 LE 特性（不可信）**
```
BluetoothHCILEReadLocalSupportedFeatures → rc=0x0 value=0
BluetoothHCILEReadSupportedStates       → rc=0x0 value=0
（同一 API 的 BluetoothHCIReadDeviceAddress 能正确读到 14:5A:41:0C:B5:10，
  说明这些 LE 读取命令本身不被该控制器/API 层支持，不能据此判定无 LE））
```

**结论**
- 控制器**能收**（LE 扫描正常）
- 控制器**不能发**（LE 广播不出现，无论走 CoreBluetooth 还是私有 API）
- 经典蓝牙可发现广播正常（iPhone 蓝牙设置里可见 Mac）
→ MT7925 在该固件/驱动组合下 **LE 广播通路不可用**，这是 AirDrop（BLE 信标）不可逾越的阻断点。

### 里程碑 13：LE 广播所有路径穷尽（2026-11-08 03:00）

| 路径 | 结果 |
|---|---|
| CoreBluetooth ADV_IND（名字 MtkTest，50s） | `isAdvertising=true`，iPhone 看不到 |
| 私有 API ADV_IND（修正签名） | 0x0/0x0/0x0，看不到 |
| 私有 API ADV_NONCONN_IND（type=3，25s） | 0x0/0x0/0x0，看不到 |
| 原始 HCI（hciadv） | API 在 macOS 26 已移除 |

**固件文件**：只有 `BT_RAM_CODE_MT7925_1_1_hdr.bin`（448,367 字节，与 Linux mt76 同名文件一致），无其他变体可换。

**最终结论**：MT7925 在该固件下能收 LE（扫描 27 个设备）但不能发 LE（所有广播路径系统层面成功、射频无包）。AirDrop 的 BLE 信标无法发出 → iPhone 永远发现不了 Mac。

**要突破需要**：MediaTek WMT 中 LE 广播使能命令的逆向，或 USB 硬件抓包确认 HCI 命令是否到达芯片。软件参数调整已穷尽。

### 里程碑 14：蓝牙 HCI 层的最终定位（2026-11-08 03:45）

**USB 端点布局（MtkBtFw 实测打印）**
```
interface 0 alt 0, 3 endpoints
  0x81   interrupt IN   mps 16    HCI 事件
  0x82   bulk IN        mps 512   ACL 入
  0x02   bulk OUT       mps 512   HCI 命令 / ACL 出（无 interrupt OUT）
```

**关键属性**
```
UsbExclusiveOwner = "pid 822, bluetoothd"
```
→ bluetoothd **独占**蓝牙 USB 设备（用户态直接驱动 HCI），
  我们的驱动只能在启动早期抢在它之前加载固件，随后必须放手。

**驱动侧 HCI 探针结果（8 轮迭代）**
- 控制端点发命令：超时
- Bulk OUT 0x02 发命令（含先发 HCI Reset）：无事件响应
- 中断 IN 0x81：修正长度(≤16)、DMA prepare、clearStall、中止后重新武装后，
  仍以 kIOReturnAborted 结束（设备已被 bluetoothd 独占）

**macOS 侧同样的失败（bt-reinit.log）**
```
OI_HCICMD_ReadLocalVersionInformation failed: STATUS 408
LE_InitMultipleAdvertising failed with status STATUS 408
LeBroadcaster.mm::stackDidStart() -- status != OI_OK -- 0x0198
```

**结论**：bluetoothd 的用户态 HCI 传输对 MT7925 不可靠 →
LE 多广播实例初始化失败 → LE 广播栈不启动 → AirDrop 信标发不出。
要修需要在 macOS 上实现/替换蓝牙 HCI 传输层，属新课题。

### 里程碑 15：蓝牙传输层架构的最终判定（2026-11-08 04:10）

**内核侧没有 USB 蓝牙传输类**
```
IOBluetoothHCIController : provider = IOResources（不是 USB 设备）
内核集合符号：无 IOBluetoothHostController / IOBluetoothUSBTransport / HCIUSB
子节点：IOBluetoothHCIUserClient（用户态 bluetoothd 通过它注入 HCI 包）
```

**用户态侧**
```
bluetoothd 内含 USB Transport（字符串：FindTransportType -- USB Transport、IOUSBHostDevice）
MT7925 设备属性：UsbExclusiveOwner = "pid 822, bluetoothd"
```

**结论：macOS 这一代的蓝牙 USB 传输完全在 bluetoothd 内部，内核里没有任何可挂钩的传输接口。**
- 自研 HCI 传输 kext：架构上不可行（没有 provider 协议可用）
- 拦截 bluetoothd 的 USB 访问：不可行（独占所有权）
- 唯一可行的办法是让 bluetoothd 的通用 USB 传输能正确驱动该芯片——即使用 macOS 已支持的蓝牙适配器

**实际可行的 AirDrop 方案（硬件组合）**
- Wi-Fi 用 MT7925（AWDL 已可用：awdl0 + 对端发现 + 数据帧）
- 蓝牙换用 macOS 原生支持的 USB 适配器（Broadcom BCM20702 / 20703 系列最佳）
- 这样 BLE 广播（AirDrop 信标）由支持的适配器发出，数据仍走 MT7925 的 AWDL

### 里程碑 16：自研 LE 扩展广播成功（2026-11-08 09:00-11:30）

**突破：绕过 macOS 蓝牙栈，驱动直接发 AirDrop BLE 信标，iPhone 能扫到。**

关键发现链（全部靠自研 HCI 探针得到）：
1. **HCI 命令通道**：控制端点（class request 0x20），且**不带包类型字节 0x01**
   —— 带类型字节时控制器回 `0f 04 01 01 01 xx`（错位），不带时回正确 `0e 04 01 <op> <op> 00`
2. **HCI 事件通道**：中断 IN 端点 0x81（最大包长 **16** 字节，读 1024 会立即 `kIOReturnAborted`）
3. 控制器能力：HCI 5.4 / MediaTek 0x0046，LE 扩展广播支持，**253 个广播实例**
4. **传统广播命令全部返回 0x0C（Command Disallowed）**；**扩展广播命令全部成功**：
   - `LE Set Extended Advertising Parameters` (0x2036) status=0
   - `LE Set Extended Advertising Data` (0x2037) status=0
   - `LE Set Extended Advertising Enable` (0x2039) status=0
5. 广播参数：`advertising_event_properties = 0x0013`（传统 PDU + 可连接 + 可扫描），
   间隔 100 ms，`tx_power = 0x00`，信道图 `0x07`
6. 广播数据（30 字节，限 31）：
   ```
   02 01 06                                     Flags
   17 ff 4c 00 05 12 40 <tag3> 00 00 00 00 03 00x8 00   AirDrop TLV（BLE-2 规范）
   02 09 4d                                    名字 "M"
   ```
7. iPhone 蓝牙调试助手可扫到 `M`（-65 dBm）

**附带修复：AWDL 发送环拥塞**
- `kAwdlTxSlots` 16 → 64，`dtx full` 从 192 降到 0

**新的核心矛盾**
- iPhone 分享面板需要 Mac 的 BLE 信标 → 只有我们的驱动能发（需独占蓝牙）
- iPhone 还需要 Mac 的 `_airdrop._tcp` AWDL 服务 → 只有 macOS sharingd 能发，且**要求蓝牙可用**
- 实测：驱动独占蓝牙时，重启 sharingd 也只在极短窗口内注册服务
→ 二者无法稳定共存；剩余方案是自行在 awdl0 上注册服务并实现 AirDrop 接收端。

### 里程碑 17：自建接收端与 iPhone 的真实交互（2026-11-08 13:00-13:40）

**关键实证（全部来自驱动 m3-log，不依赖推测）**

1. **iPhone 通过 AWDL 主动向我们发 mDNS 查询**：
```
awdl data rx len=294 ch=6 fc=0800 da=3333000000fb sa=8a7f5ede444b et=86dd
```
`da=33:33:00:00:00:fb` = IPv6 mDNS 组播，来源是 iPhone 的 AWDL MAC。

2. **iPhone 连过我们的自建 AirDrop 服务（TLS）**：
```
accept from fe80::887f:5eff:fede:444b        （= 8a:7f:5e:de:44:4b 的 EUI-64）
tls handshake failed: Connection reset by peer
```
握手被对端重置，待查（证书/算法/时序）。

3. **自建方案已完整搭起**：
- `awdl-svc/awdlsvc.c`：用 `DNSServiceRegister` + `kDNSServiceFlagsIncludeAWDL` 注册 `_airdrop._tcp`
  （服务能出现在 awdl0，interface 10）
- `awdl-svc/airdrop_rx.py`：TLS 服务器 + `/Discover` `/Ask` `/Upload`（自签名证书，本地验证通过）
- 同时保存了 Apple 二进制 plist 的发现请求（`SenderRecordData` 3718 字节 PKCS#7 + `DeviceSupportFlags` 111611）

**当日驱动改动**
- 保留：`kAwdlTxSlots` 16 → 64（AWDL 发送环，`dtx full` 从 192 降到 0）
- 已回退：AWDL 组播地址判断（改 addr1 的实验版本，13:32 构建，未安装）

**遗留**
- iPhone 的 TLS 握手被重置：需要证书/密码套件/时序层面的进一步对比
- Wi-Fi 偶发 `AP refused authentication`：驱动仅支持 WPA2-PSK，需路由器关闭 WPA3

### 里程碑 18：TLS 打通，卡在 /Discover 之后（2026-11-08 13:40-13:50）

**里程碑：iPhone 与我们的接收端完成 TLS 1.2 握手**

```
13:44:47 tls TLSv1.2 ECDHE-RSA-AES256-GCM-SHA384
13:44:47 TLS ok from fe80::1029:e1ff:fefa:9804
13:44:47 POST /Discover body=3834      (DeviceSupportFlags + SenderRecordData)
13:44:47 Discover response: 169 bytes  (ReceiverComputerName/ModelName/MediaCapabilities)
13:45:17 Connection reset by peer      (对端 30 秒后重置)
```

即：**自签名证书被接受、TLS 协商成功、/Discover 请求收到**。剩余问题在我们的
`/Discover` 响应没有被对方采纳（没有继续 `/Ask`）。

**另一侧观察**
- 本机 AWDL 浏览只能看到自己的服务，看不到 iPhone 的 `_airdrop._tcp`；
- Finder 隔空投送里能看到 iPhone（来自 BLE），但拖文件过去时 AWDL 单播计数保持
  `uc=0/0` —— macOS 没有发起任何 AWDL 数据连接。
- 组播发送：`grp=22/76`（约 29% 成功），单播 `uc=0/0`。

**结论**：接收方向（iPhone → 我们的服务）已经走到 TLS + /Discover；发送方向
（Mac → iPhone）仍卡在无法解析 iPhone 的 AWDL 地址。

### 里程碑 19：AirDrop 完整接收成功（2026-11-08 14:35-14:55）

**首次完整接收成功**：MacBook Pro 2019（macOS 12.4）→ 本机，
`/Discover` → `/Ask` → `/Upload` 全部走通，收到 238,657 字节 PNG（1178x678），
gzip + CPIO(odc) 解包验证无损。

**可用组合（三个组件同时满足）**
1. `MtkBtFw.kext`：加载固件 + 发可连接 LE 扩展广播（AD 含 Flags/AirDrop TLV/名字 M），
   并**独占蓝牙 1 小时**（`bt-state=loading`，macOS 蓝牙 Off）
2. `awdlsvc`（`advertise-only`）：`DNSServiceRegister` + `kDNSServiceFlagsIncludeAWDL`
   注册 `_airdrop._tcp`，端口 8770
3. `airdrop_rx.py`：TLS 1.2 自签名证书 + `/Discover` `/Ask` `/Upload`
   （支持 `Expect: 100-continue`、chunked、`Content-Length`；收到后自动 gzip+CPIO 解包到 `~/Downloads`）

**实测数据**
| 项目 | 结果 |
|---|---|
| TLS 版本/套件 | TLSv1.2 ECDHE-RSA-AES256-GCM-SHA384 |
| `/Ask` 传输（6 Mb/s 固定速率） | 139 秒（超过发送方 45 秒超时 → 失败）|
| `/Ask` 传输（12 Mb/s） | 3–11 秒 |
| 完整传输（230 KB） | 88 秒（成功一次）|
| AWDL 组播 TX | 27 成功 / 25 失败（约 52%）|
| AWDL 单播 TX | 2 成功 / 26 失败（约 7%）← 稳定性瓶颈 |

**已知瓶颈**：AWDL 单播数据帧缺少 station record（`wcid` 仍是 AP 的），
硬件收不到对端 ACK → 重传/中断（`No route to host`）。修复需要在驱动里为 AWDL 对端
下发 STA_REC（MCU 命令层面，属上游工作范围）。

### 里程碑 20：稳定性实验与结论（2026-11-08 14:45-15:15）

**提速有效**：AWDL 数据帧固定速率从 OFDM 6 Mb/s 提到 12 Mb/s（TXD 速率索引 20），
`/Ask`（103 KB）从 **139 秒** 降到 **2–11 秒**，`/Upload`（7 KB）约 1 秒完成。

**第二次完整接收成功**（12 Mb/s + 自动解包）：
```
POST /Ask body=18477      (2 s)
POST /Upload body=7223    (1 s)
extracted 截屏2026-11-08 14.52.23.png 7010 bytes → ~/Downloads
```

**station record 实验（已排除）**
- 为 AWDL 对端下发 `STA_REC_UPDATE`（`CONNECTION_IBSS_ADHOC = 0x40004`, wcid 20,
  `muar_idx = 1`）→ 固件接受（`rc=0`），但单播发送 **0 成功**（`uc=0/56`），比用 AP wcid 更差。
- 按信道修正 PHY 速率集（5 GHz 用 OFDM）后仍为 `uc=0/6`。
- 结论：**ACK 失败不是缺少 station record**，问题在固件对 OMAC 单播/ACK 的处理。
  已把 `awdlStaOn` 默认关闭，回到 AP wcid 路径。

**最终可用配置**
- `MtkBtFw.kext`：LE 扩展广播（可连接）+ 独占蓝牙 3600 秒
- `awdlsvc advertise-only` + `airdrop_rx.py`（TLS 1.2 + Discover/Ask/Upload + 自动解包）
- 核心驱动：`kAwdlTxSlots=64`、AWDL 数据 12 Mb/s、AP wcid 路径
- 实测：小文件 1–3 秒完成；大文件（230 KB）约 88 秒，偶发链路中断

### 里程碑 21：IBSS BSS 实验——单播 ACK 的真实原因（2026-11-08 15:30-15:45）

**实验**：为 AWDL 链路补上完整的 BSS 上下文
- `BSS_INFO_UPDATE` bss_idx=1, `CONNECTION_IBSS_ADHOC (0x40004)`, omac_idx=1 → **固件接受 rc=0**
- `STA_REC_UPDATE` 对端 wcid=20 → **固件接受 rc=0**
- 数据帧改用 wcid 20

**结果：单播仍然 0 成功**（`uc=0/15`），逐帧状态：
```
txfree awdl pid=4 st=1 tries=15 ch=149 win=1
```
即：每帧都重传满 15 次、全在同一信道上、全部无 ACK。

**关键发现**：
```
awdl sync: roc fail ch=44 grant=0 status=255 after 309 ms
```
- 驱动无法在信道 44 上启动 ROC（status=255，监管域/DFS 限制）
- 对端在 6 / 44 / 149 之间跳，我们只能覆盖 6 / 149 / 157

**结论**：单播 ACK 失败的根因是 **AWDL 时隙/信道调度对不齐**，不是 station record、也不是速率。
这是上游 AWDL 窗口调度器的核心难题。

**已回退**：`awdlStaOn=false`（回到 AP wcid 路径），速率保持 12 Mb/s（`/Ask` 最快）。
IBSS BSS + STA_REC 代码保留在驱动里（默认关闭，可用 `itl_awdlsta=1` 打开）。

### 里程碑 22：AWDL 窗口时序修复成功（2026-11-08 20:20-21:25）

**核心突破：窗口不再迟到。**

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `avglate`（平均迟到） | 43–53 ms | **1.6–1.75 ms** |
| `late`（迟到窗口数） | 541–1300 | **11–69** |
| `dwell`（实际驻留） | 被压到 1 ms | **98.9 ms** |
| `rolled`（滚动对齐次数） | 0 | **95–281** |

**根因链**（三次递进）：
1. Family 交来的窗口 `startUs` 常常已经过去了，`setupAt = startUs - lead` 落在过去 → 等待被跳过 → 38ms setup 后迟到 40ms+；
2. 判据应为 `now + leadUs > startUs`（"还来得及开始 setup 吗"），而非 `startUs < now`；
3. **`awdlAwPeriodUs` 是 AW 长度（16 TU = 16384µs），不是 AWDL 周期**——滚动必须用 102400µs（100 TU）。这一步是决定性的：此前 roll 的周期门槛 20000 把 16384 拒掉，`rolled` 始终为 0。

**当前状态**
- 管理帧发送：`tx=399/400`（99.8%）
- 对端动作帧接收：`af=682`，`peer4` 正常
- 发现/传输仍然**断断续续**

**剩余堵点：AWDL 树未合并**
- `role=1`，`master`/`top` 都是自己；对端也是自己的根；
- 驱动侧该做的都做了：AW-end 消息（`0x2b`）在发、动作帧已交给 peer manager、选举 TLV 已解析（`itlmt-awdl-peer-el`）；
- 选举在 Family（wifip2pd）的 `runElection` 内，结果只通过 `setAWDL_SYNC_STATE` 告知驱动——**驱动无权决定**。

**环境变量**：对端与本机 Wi-Fi 频段不同（2.4G vs 5G）时，AWDL 同步明显更差；切到同频段后设备才出现。

### 里程碑 23：AirDrop 完整传输打通——真因是 TXT flags 声明了没实现的能力（2026-11-08 23:00 ~ 11-09 01:23）

**结果**：MacBook Pro 2019 / macOS 12.4 → 本机，7,010 字节 PNG 完整落地 `~/Downloads`，`/Discover` → `/Ask` → `/Upload` 全程约 1 秒。
校验：`PNG image data, 136 x 72, 8-bit/color RGBA`，gzip 完整，内部 cpio `070707`（odc）。

**真因**：`awdl-svc/awdlsvc.c` 注册 `_airdrop._tcp` 时写的是 `flags=141`（`0x08d` = URLSharing | **Pipelining** | MixedTypes | DiscoverMaybe）。
接收端从未实现 UP-8（提示期间缓冲 body、接受后处理），等于宣告了做不到的能力（MDNS-5 明令禁止）。

**失败形态（下次直接认这个指纹）**
```
POST /Upload ... totalbytes: 7010
  chunkhdr#1 b'A'        ← 0x0A，正好 10 字节：gzip 头 1f 8b 08 00 00 00 00 00 00 03
  chunkhdr#2 b''         ← 1 秒后 EOF，没有 0\r\n 结束块
gzip failed: Compressed file ended before the end-of-stream marker
```
Mac 侧显示「已取消拷贝」。此时**改我们的响应内容或时机全部无效**：提前回 200、回 `100 Continue`、完全不响应，三种都死在同一处；
每秒采样 `raw rx` 无跳变，说明那 7,010 字节**根本没上过空口**——不是丢包，是发送方拒绝发送。

**改法**：`flags=141 → 137`（`0x089`，MDNS-5 给最小接收方的推荐值）。清掉 Pipelining 后 UP-3 生效——发送方自己加上
`Expect: 100-continue`（这个头之前从未出现过，就是开关的指纹），我们回 `100 Continue`，body 一次性 7,265 字节发完。

**时间线（这个 bug 藏了三小时的原因）**
| 时间 | 事件 |
|---|---|
| 11-08 14:39 | 里程碑 19 传成功 238,657 字节 |
| 11-08 16:11 | `awdlsvc` 被改成 `flags=141`，注释写着「让发送方可流水线上传」 |
| 11-08 23:00 ~ 11-09 01:23 | 每次 `/Upload` 都死在 10 字节，反复误判成 AWDL 单播链路问题 |

**顺带确认的协议细节**
- macOS 12.4 用**旧版** `/Ask`：没有 `TransferID`、`TransferType`、`FileSize`。`id:` 为空是正常的，不能用 UP-9 的比对去要求它；
- `/Ask` 必须**读完 body 再回 200**（提前回会把 CFNetwork 的请求/响应状态机打乱）；
- `/Upload` 按 UP-3：有 `Expect` 才回 `100 Continue`，收完 body 再回 `200 + Content-Length: 0 + Connection: close`。

### 里程碑 24：持续传输的墙——48~131 KB 之间塌掉（2026-11-09 01:25-01:31）

**现象**：同一条链路，小文件秒过，238,657 字节的图三次全部停在中途，停点还不固定：

| 时刻 | 对端行为 | 停在哪 |
|---|---|---|
| 01:25 | `/Ask` body 103,892 B @ 13,037 B/s，然后 `/Upload` `chunkhdr#1 b'20000'`（= 131,072） | payload 零字节，静默 105 秒 |
| 01:29 | `/Ask` body 103,892 B @ 5,057-15,237 B/s | 49,152 / 103,892 后静默 |
| 00:50（更早） | `/Ask` body 103,892 B | 65,536 / 103,892 后静默 |

**停滞期读数**（10 秒窗口）
```
awdl0 Ibytes      +0      没有一个字节进到网络栈
驱动 rx           +61     空口上确实有帧到达
syncs             +4      对端同步帧还在传，L2 没断
TCP out-of-order  0       栈里没有重传副本
```

**只剩两种解释，需要驱动计数器分开**
1. **对端停发**：它的 AWDL 会话判定不可用，收手等重连——失败后设备立刻从列表消失，支持这一条；
2. **我们丢帧**：帧到了无线电（`rx` 在涨），但在送进网络栈前被丢掉，所以 `awdl0` 零字节、TCP 也看不到重传。

判定实验：RX 路径加「按类型收到帧数 / 真正交给栈的帧数」两个计数器。差值大 = 我们丢帧；数据帧直接归零 = 对端停发。

**2026-11-09 下一步已准备**：`mt7925-upstream` 的 core RX 新增 `awdl drx path: candidate/badsnap/parsed/queued/ringfull` 日志；Native 现有 `drx=in/up/notus/fail/dup`。编译 `itlmt7925` 成功。下次复现时在停滞前后各读一次两组累计值：`candidate` 不涨表示主机未收到匹配 AWDL BSSID 的数据帧；`candidate` 涨而 `parsed` 不涨看 `badsnap`；`parsed` 涨但 `queued` 不涨看 `ringfull`；`queued` 涨但 Native `drx in` 不涨看 core→Native 队列；Native `in` 涨而 `up` 不涨看 `notus/fail/dup`。这些计数只覆盖 ROC 活跃期间、已到驱动的帧，无法单凭归零证明对端无线电完全没有发射。
这与上游报的「5 GHz 单播约 71% 失败」是同一堵墙。

**接收端观测改进**（`awdl-svc/airdrop_rx.py`，保留）
- chunked 读取改 `read1` 8 KB 分片、每 32 KB 打进度、中途断掉打 `chunk#N EOF at X/Y`；
- body 阶段超时 25 s（请求之间恢复 120 s）。注意：25 s 超时关连接，Mac 会显示「拒绝了你的请求」——
  接收端超时在发送端 UI 上等同于拒绝，想观察对端多久自己放弃，超时必须长于对端。

### 2026-11-09：蓝牙探测模式说明

当前 `MtkBtFw` 的 `hciProbe()` 有意保持 USB 接口 21600 秒，以发送和扫描 AirDrop BLE 信标；因此 `bt-state=loading`、系统蓝牙 Off 是探测模式的预期表现。曾尝试把它改为固件加载后立即交还系统，并更新 `BtLeDlFix`，但这会取消正在使用的探测能力。用户指出该约束后，已在**尚未重启**时把 EFI 中的两份驱动从 `Kexts-backup` 时间戳 `20261109-193459` 恢复，SHA-256 校验通过，源码也恢复。该尝试不应视为已验证的蓝牙修复。

### 2026-11-09：MacBook 上的发现间歇，定位到 AWDL 角色振荡

本机当前 BLE 探测驱动已成功启动 AirDrop 信标（`LESetExtAdvEnable status=0`）；`awdlsvc advertise-only`、`airdrop_rx.py` 和 LaunchAgent 都在运行。开机后已收到 MacBook 的 `/Discover`，说明发现链至少曾经通畅。连续 25 秒浏览 `_airdrop._tcp` 只见 Add，没有 Remove；看护日志也没有重启注册的记录。

同时每 2 秒采样 `itlmt-awdl`，观察到 `role=2 master=e69e053cb192` 与 `role=1 master=<本机>` 约每 4 秒交替，`chg` 从 86 增至 92。对端 `peer4` 与 `syncs` 持续增长。源码中 `setAWDL_SYNC_STATE()` 无条件接受 Family 的 self-master 结果，覆盖 `awdlElectionConsider()` 根据更强对端作出的驱动侧跟随决定；下一对端帧又重新跟随，解释了角色振荡。

已修改 `itlmt7925Native`：跟随对端且最近 8 秒收到其选举帧时忽略 Family 的 self-master 覆盖；对端失联后仍允许 Family 接管。新增 `fself` 计数。编译及安装校验通过，已备份旧 Native 到 `EFI/OC/Kexts-backup/itlmt7925Native.kext.20261109-194116`。**待重启验证**：预期 `fself` 增长、`fol=1` 时 `role=2` 持续，`chg` 不再每几秒增长；再看 MacBook AirDrop 列表。

**重启后 19:45 验证**：MacBook 已能看到本设备。20 秒、每 2 秒采样，`role=2 master=e69e053cb192` 始终不变，`chg=1` 不增长，`fself=22→27`，`peer4=284→348`。说明 Family 仍在尝试改回 self-master，但补丁正确挡住了覆盖；AWDL 同步和对端帧持续。`airdrop_rx.py` 在 19:44:17、19:44:34、19:45:12 收到并应答 MacBook 的 TLS `/Discover`。当前没有 `/Ask` 或 `/Upload`，因此尚未验证完整传输。core `awdl drx path` 显示 `candidate=425 parsed=425 queued=425 ringfull=0`，接收路径无该层丢弃；`awdl txfree` 的单播仍是 `uc=0/4 ucheard=0/70`，大文件可靠性问题未解决。

### 2026-11-09：探索 macOS 26 系统蓝牙路径（本机未切换）

19:47 MacBook 又看不到本设备，但驱动仍 `role=2`、`chg=1`、`fself=59`，`peer4=754`，服务进程仍在，日志显示 MacBook 于 19:47:26 再次发起 `/Discover`，请求 body 还未结束。故角色振荡已经解决，消失现象另有原因；core 单播 TX 仍 `uc=0/5 ucheard=0/72`，不能把后续不稳定全归因于蓝牙。

**对先前“找到可行路径”的更正**：系统蓝牙接管并非新发现，也未被证明能在当前 25G332 上稳定完成 AirDrop。里程碑 11–15 已记录：`State: On` 时 CoreBluetooth 报告广播成功但空中无 LE 包；后续 `bluetoothd` 报 `ReadLocalVersionInformation STATUS 408`、多广告初始化失败，USB 由它独占，最终因此改走驱动直发 BLE 信标。另在 25G241 上，`BtLeDlFix 1.2.0` 曾使系统蓝牙的 LE 广播真正上空口、BLE 鼠标可用，但没有完成 AirDrop 稳定传输验证。这两组记录必须同时看，不能把控制器 On 或补丁字节匹配等同于 AirDrop 修复。

可供**受控复测**的系统蓝牙路径是：`MtkBtFw` 下载固件后关闭 USB interface，交给 `bluetoothd`；`BlueToolFixup` 处理第三方控制器，`BtLeDlFix` 补 LE 数据长度等待和扩展广告。当前系统是 macOS 26.7.2 / 25G332，而原 `BtLeDlFix 1.2.0` 的第三个广告补丁只匹配 25G241；分析本机 `/usr/sbin/bluetoothd` x86_64 后发现调用位移从 `3e 6b ce ff` 变为 `1e 6c ce ff`。已加入 25G332 专用匹配，四个必要模式在当前 binary 中均唯一命中且不跨页；这只证明静态匹配，尚未证明运行时 HCI/空口行为。

`mt7925-btfix/MtkBtFw` 新增 `mtkbt_system=1` 启动参数：有参数时固件初始化后交还 USB 接口；无参数时保留原有六小时 BLE 信标/扫描探测。系统模式试验要另一次启动，并在确认系统 AirDrop 服务已发布后停止自建 `awdlsvc` 注册，避免同类型服务混淆。验证顺序：`bt-state=ready`、系统蓝牙真实地址/On、`bluetoothd` 扩展广告成功、MacBook/iPhone 发现与 `/Discover`、最后再测传输。若系统模式失败，去掉启动参数即可回到探测模式；旧 EFI 驱动备份仍在。

**19:54 复测已部署，待重启**：用户授权沿此方向继续。EFI 已装 `MtkBtFw 0.1.1`、`BtLeDlFix 1.2.0`，启动参数加 `mtkbt_system=1`；`BlueToolFixup 2.7.3` 与两项蓝牙 NVRAM 值原本已正确。旧驱动备份为 `Kexts-backup/{MtkBtFw,BtLeDlFix}.kext.20261109-195448`，配置备份为 `config.plist.bt-system-20261109-195448`，复制后 SHA-256 与 plist lint 均通过。重启前当前运行内核仍是旧探测模式。下次开机先判断 `bt-state`、真实地址/On、bluetoothd 广告命令与空口信标；不能仅凭 `State: On` 宣布成功。若要回退，恢复该配置和两份 kext，或至少去掉 `mtkbt_system=1` 以恢复六小时探测。

**20:04 重启后初查**：Wi-Fi 并非完全无法连接，用户更正为约 3 分钟后连上；当前 `en1` 是 `192.168.0.217`、`wifi-state=connected`。启动早期 m3-log 有多次 `auth-fail`、`no response fc=b0`，选择了 5 GHz 信道 153/157 的不同 BSSID 后最终连接。不能仅凭时间相邻断言蓝牙改动造成 Wi-Fi 慢连。蓝牙固件驱动为 `bt-state=ready`，日志确认 `system mode: firmware ready, handing USB interface to bluetoothd`，但 `system_profiler SPBluetoothDataType` 仍无控制器，所以系统蓝牙目标目前失败。曾准备回退脚本，管理员授权窗口被用户取消，**EFI 未回退**；当前继续运行系统模式，下一步应检查 `bluetoothd` 是否独占 USB、为何未识别设备，再决定是否回退。不要将本轮标记为成功。

**20:07 蓝牙细查**：MT7925 USB 设备 `0x0e8d:0x0717` 正常枚举，接口 0 `BT_ACL_If` 的 MtkBtFw 下载了 3 段固件并收到 HCI Reset 完成事件，约 3.2 秒后报告 `ready`，随后关闭接口。`IOBluetoothHCIController` 存在并标 `BluetoothTransportConnected=Yes`，但这是通用控制器对象；`system_profiler` 仍报告 `controllerInfo == nil`，不能视为硬件已就绪。`bluetoothd` 的 launchd `runs=26`、`last exit code=1`，持续重启。最新 IPS 是 `EXC_GUARD / GUARD_TYPE_USER`、`Application Triggered Fault`；19:28 等**探测模式时期**的 IPS 具有同一调用栈，因此不能归咎于本次 25G332 补丁。当前 USB 设备/接口的 I/O Registry 未显示 `UsbExclusiveOwner=pid ..., bluetoothd`，与旧记录的成功认领状态不同；需要进一步确定为何未取得设备，现有证据不足以判定是 USB transport、bluetoothd 启动阶段还是补丁运行时问题。

**后续定位实验**：检查 `/Library/Preferences/com.apple.MobileBluetooth.debug.plist` 发现 HCI 文件追踪未启用，root 蓝牙日志目录为空。临时设置 `HCITraces={StackDebugEnabled,HCIFileTraces,HIDTrace=true}` 并 `launchctl kickstart -k system/com.apple.bluetoothd`，12 秒后仍无 `bluetoothd-hci-latest.pklg`，已恢复原调试 plist。这与 USB interface 未显示 `UsbExclusiveOwner=bluetoothd` 一起提示问题可能在 HCI trace 生成之前，但也可能是 25G332 不再遵循旧调试开关，不能据此断言没有 HCI 命令发出。要确定：同一 `mtkbt_system=1` 条件下 A/B 禁用 `BtLeDlFix`（先排除补丁运行时），看 bluetoothd 重启次数和 USB 独占权；如仍失败，用 USB 层抓 HCI control transfer 0x1001 与 interrupt 0x81 响应，或对 crash offset 符号化。每次只改一个变量。

**20:13 A/B 已准备，待重启**：仅将 EFI `Kernel/Add/BtLeDlFix.kext` 设为 `Enabled=false`，保留 `mtkbt_system=1`、`MtkBtFw 0.1.1`、`BlueToolFixup 2.7.3`、Wi-Fi/AWDL 驱动与蓝牙 NVRAM 配置。原配置备份为 `config.plist.btledl-ab-20261109-201331`，plist lint 通过。下次启动比较 `bluetoothd` 的 `runs/last exit code`、`system_profiler SPBluetoothDataType` 和 USB `UsbExclusiveOwner`。若 `bluetoothd` 仍频繁退出，则 LE 补丁并非唯一原因；若稳定且系统蓝牙 On，再按原配置恢复 BtLeDlFix 并逐项检查广告补丁。

**20:24 A/B 重启后结论**：`BtLeDlFix` 确实未加载，`BlueToolFixup 2.7.3` 与 `MtkBtFw 0.1.1` 已加载，`mtkbt_system=1` 仍在启动参数。固件 `bt-state=ready`，MT7925 USB `0x0e8d:0x0717` 仍枚举，但 `system_profiler` 的 `controllerInfo == nil`；`bluetoothd` 多次以 exit code 1 重启，USB 设备上没有 `UsbExclusiveOwner=bluetoothd`。因此禁用 LE 补丁没有恢复系统蓝牙，不能把当前故障归因于 `BtLeDlFix`。实时日志比先前静态检查更明确：`External Bluetooth dongle was not found -- setting chipset to default BCM_4350C2`、`HCI Transport is set to H4BC`，随后等待不存在的 `/dev/cu.BLTH` 15 秒并报 `Transport layer initialization failed`。历史成功日志 `bt-le-clean.log` 中同一设备会先被 `Found_USB_Dongle` 枚举、`FoundBluetoothInterfaces(...) returned true`，然后选 `THIRD_PARTY_DONGLE` / `USB`，并由 `bluetoothd` 独占。历史与当前设备的 `USBPortType` 均为 0，不能据此修改 USB 映射。当前 NVRAM `bluetoothExternalDongleFailed=01`，手动清零后短时间又变 01；这可能是反复初始化失败后重新写入，尚不能判断因果。曾尝试完整停止 `bluetoothd` 后清零再启动的受控实验，但管理员命令未返回，已中止，未观察到任何状态改变。下一步应先核实启动时的 USB 发现时序与失败标志的读取条件，再决定是否改驱动/配置；保持每次仅改一项。筛选后的实时日志：`bluetooth-transport-20261109.log`。

**20:29 下一轮驱动试验已安装，待重启**：发现 `MtkBtFw::start()` 将固件加载交给后台线程后立即返回，而 `bluetoothd` 日志显示其启动时调用 `IOKitWaitQuiet()`，然后寻找 USB 控制器。两者可能竞态；现有日志尚不能证明这是唯一原因。`MtkBtFw 0.1.2` 在 `mtkbt_system=1` 时通过 `IOService::adjustBusy(1/-1)` 将 USB 固件加载/接口关闭纳入 IOKit busyState，令 `bluetoothd` 的等待覆盖该后台阶段；无该启动参数时六小时探测模式不持有 busyState。线程启动失败也会撤销 busy 标记。构建通过，只有原有的整数宽度警告，plist lint、EFI/构建二进制 SHA-256 对照通过。EFI 中仅替换 `MtkBtFw.kext`，旧版备份为 `EFI/OC/Kexts-backup/MtkBtFw.kext.20261109-202856`；`BtLeDlFix` 仍禁用，`BlueToolFixup` 仍启用，NVRAM/Add 的失败标志仍为 `00`。下次重启检查是否出现 `Found_USB_Dongle` / `HCI Transport is set to USB`、`UsbExclusiveOwner=bluetoothd`、真实地址及系统蓝牙 On。若仍选 H4BC，继续追查 NVRAM 标志/USB 扫描，不要把该假设视为已验证。卸载 0.1.2 时从上述备份恢复 kext。

**20:37 承接 20:29 的 busy 标记试验：实现已复核、构建完成，并顺带修掉一个会中断固件的核心 bug**

承接上文 20:29 的 `MtkBtFw 0.1.2`：
- 实现位置已复核：`start()` 在 `registerService()` **之前**调用 `adjustBusy(1)`，`worker()` 在 `run()` 结束后调用 `adjustBusy(-1)` 再 `release()`；`startThread` 失败路径也撤销标记。布尔量 `waitQuietHeld` 只在 `PE_parse_boot_argn("mtkbt_system")` 为真时置位，六小时探测模式不持有 busyState——与 20:29 的设计一致。
- 构建产物：`build/MtkBtFw.kext`，二进制 **501,616 字节**（20:35），`CFBundleVersion 0.1.2`；编译仅剩既有的整数宽度警告。
- 这条试验要检验的正是 20:24 实时日志里的那句话：`External Bluetooth dongle was not found` 之后 `HCI Transport is set to H4BC`、等待 `/dev/cu.BLTH`。若竞态假设成立，`bluetoothd` 的 `IOKitWaitQuiet()` 应等到固件加载与接口关闭完成，随后走 `Found_USB_Dongle` → `HCI Transport is set to USB` → `UsbExclusiveOwner=bluetoothd`。若仍落到 H4BC，则该假设被否证，按 20:24 的结论继续查 NVRAM 失败标志与 USB 扫描顺序。

同轮从上游文档接出一条独立线索（**与蓝牙无关，但会打断固件**）：上游 §10.84（核心 `2773F995`）发现 `sniffer config` 的 `bw` 字节误用了 `fastBw`（`CMD_CBW_*`：0=20/1=40/2=80/3=160），而固件要的是 sniffer 自己的 `ch_width` 表（20/40→0，80→1，160→2，80+80→3，320→6）。在 160 MHz 的 AP 上固件因此收到"80+80 且 center2=0"，**停止响应 MCU 命令**（`sniffer cfg rc=-110`、`firmware not answering MCU commands`、固件重载）。
- 核对本机 `itlmt7925_m3.cpp::m3SnifferConfig()`：写法与上游修复前完全相同（`v[3] = apWidth ? fastBw : 0;`），同一个 bug 存在。
- 已改为查表 `kSnifferBw[4] = {0, 0, 1, 2}`，离信道窗口仍固定 20 MHz（0）。构建产物 **1,845,352 字节**（20:37）。
- 这条修复与历史记录的关系：本机多次出现"芯片无响应、必须彻底断电才恢复"（`uhw 70028610 read failed`、`chip id read failed`），此前一直归因于蓝牙固件；现在至少多了一个 Wi-Fi 侧的可疑来源——AP 若工作在 80/160 MHz，sniffer 配置会把固件逼死。**尚未验证**是否会减少断电需求，需要长时间运行观察。

**一次待重启的合并安装（EFI `disk1s1`）**：
- `MtkBtFw.kext` → 0.1.2 / 501,616 字节（busy 标记）
- `itlmt7925.kext` → 20:37 / 1,845,352 字节（sniffer 宽度）
- 两者均已备份旧版到 `Kexts-backup/*.20261109-2038`；`BtLeDlFix` 保持禁用、`BlueToolFixup 2.7.3` 保持启用（延续 20:24 的 A/B 结论：禁用 LE 补丁并未恢复系统蓝牙，它不是唯一原因）。

**重启后按顺序读，不要跳步**：
1. 蓝牙：`bluetoothd` 的 `runs/exit code`、`system_profiler SPBluetoothDataType` 是否出现真实地址、USB 是否 `UsbExclusiveOwner=bluetoothd`、日志是否从 H4BC 变为 `HCI Transport is set to USB`。
2. Wi-Fi/固件：`m3-log` 是否还有 `sniffer cfg rc=-110`、`wifi-events` 是否还有 `firmware not answering MCU commands`。
3. 只有前两项都正常，才回到 AirDrop 链路（`role`、`syncs`、单播 `uc=`）。
4. 若蓝牙仍失败，按 20:24 的结论继续查 NVRAM 失败标志与 USB 扫描时序，**不要把本轮记作成功**。
