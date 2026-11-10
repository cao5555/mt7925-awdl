# AirDrop / AWDL engineering notes — MediaTek MT7925 on macOS

English translation of the working log kept during the project (`FINDINGS.zh.md` is the original).
Everything below was measured on the hardware listed, not inferred.

## Test environment

| Item | Value |
|---|---|
| Host | AMD Ryzen 7 9800X3D / Colorful B850M-PLUS |
| OS | macOS 26.7.2 (25G332, Darwin 25) |
| Wi-Fi | MediaTek MT7925 (AMD RZ717, PCI `14c3:0717`) |
| Bluetooth | MT7925 USB side (`0E8D:0717`) |
| Driver | self-built MtkWlan (0.12.1 sources + probe patches), later `thanhnndev/mt7925-macos` `dev/awdl` |
| Test phone | iPhone 14 Pro, iOS 26.1 |
| Phone state | AirDrop sheet open, Bluetooth on, Low Power Mode off, screen always on, "Everyone (10 min)" |

---

## Milestone 1 — monitor mode and channel control ✅

* Commands: `MCU_UNI_CMD_SNIFFER (0x24)` + `UNI_SNIFFER_ENABLE (tag 0)` + `UNI_SNIFFER_CONFIG (tag 1)`.
* Evidence: about 30 beacons/s on channel 6, about 9 beacons/s on 157.
* Code: `monitorOn()` / `monitorOff()` in `MtkJoin.cpp`.

## Milestone 2 — raw frames reach user space ✅

* Transport: IOUserClient ring buffer (`kMtkUcMonRead = 6`), length passed in the input struct
  (MACH caps inline data near 4 KB).
* Evidence: real beacons (`sub=8`) and probe responses (`sub=5`) with valid MACs.
* Code: `MtkUserApi.h` (`mtk_mon_batch`), `MtkUserClient.cpp`, `monCapture()` / `monDrain()`.

## Milestone 3 — first AWDL frame captured ✅ (2026-11-07 18:32)

* Channel **149**.
* Sample: `7f 00 17 f2 08 10 03 00 …` (category 127, Apple OUI, OUI type 08 = AWDL).
* Sender `a2=6a:c5:f5:5c:ce:04` (the iPhone's randomised AWDL MAC), destination
  `a1=ff:ff:ff:ff:ff:ff` (broadcast, the hallmark of an AWDL sync frame).

### Tool: `mtkmon`

```
mtkmon <channel>     park on the channel and enable sniffing (root)
mtkmon off           disable sniffing
mtkmon disconnect    drop the link and disable auto-reconnect (root)
mtkmon read [n]      drain n batches (up to 4 frames x 512 bytes each)
```

### Build / install / restore

```bash
cd mtk-src && ./tools/build-mac.sh          # CLT is enough, reproducible
sudo bash ../swap_driver.sh                 # install to the ESP (backs up the original)
sudo bash ../switch_to_eth_mode.sh          # drop mtkfe=native so disconnect works
sudo bash ../restore_driver.sh              # restore the original driver
sudo bash ../restore_native_mode.sh         # restore the native Wi-Fi front end
```

### Lessons from that phase

1. ROC does not tune the radio while unassociated — the SNIFFER command is the only way.
2. The driver had no channel-set command at all; scanning goes through the firmware's hw_scan.
3. IOUserClient struct output above ~4 KB returns `MACH_RCV_TOO_LARGE`; keep batches small.
4. `structureOutputSize` reports 0 for out-of-line buffers: pass the length yourself.
5. `monCapture` must replicate `rxFrame`'s bad-frame checks (FCS/AMSDU/ICV) or it collects garbage.
6. Frame data starts at `p + rxFrameOffset(rxd1, rxd2)`, not at the buffer start.
7. `MtkMonTotal` only refreshes when `monDrain` runs — a stale value does not mean a stall.

## Milestone 4 — full AWDL frame decode ✅ (2026-11-07 18:41)

Decoded the iPhone's **Master Indication Frame (MIF)**:

```
7f 00 17 f2 | 08 10 03 00 | 81 87 84 6b 3d 87 84 6b | 04 49 00 | 95 19 00 00 00 10 00 6e 00 00 18 …
```

| Field | Value |
|---|---|
| AF type / version | 0x08 / 0x10 (16) |
| subtype | 0x03 = MIF |
| phy_time − target_time | tx_delay = 68 µs |
| TLV type | 0x04 = Synchronization Parameters (len 73) |
| tx_chan | 0x95 = **149** ✅ matches the sniffing channel |
| tx_counter | 25 |
| aw_period / AF period | 16 / 110 |
| flags | 0x1800 |

Reference: Wireshark `packet-awdl.c`, `dissect_awdl_action()` and `awdl_tag_sync_params()`.

## Milestone 5 — AWDL link-layer node (2026-11-07 19:58)

Own C module (`mtk-src/tools/awdl/`, `awdl.h` / `awdl_frame.c`):

* builds and parses action frames per SPEC §3.2 (TLVs 4/5/6/7/12/16/18/21/24);
* BSSID fixed at `00:25:00:ff:94:73` (AWDL-1);
* only follows masters that satisfy AWDL-17 (MIF + Version TLV);
* master clock tracking and slot-aligned transmission (`down_tu` / `aw_seq`);
* channel hopping across 6/149/157 following the master's channel sequence.

Measured: `txOk` kept growing, `txFail=0`, hop cadence correct (slot 8→6, 9→157, 10→149, 11→157).

### The real bottleneck

`owly-SPEC.md` (41 KB, in the workspace) says:

* MDNS-1: a receiver must advertise `_airdrop._tcp` on **awdl0** (TXT flags ≥ 0x089);
* NET-2: neighbour addresses derive from the MAC, no ND needed;
* TLS-1: TLS 1.2/1.3 plus HTTP/1.1 `/Discover` `/Ask` `/Upload`.

Our driver had no `awdl0`, only an Ethernet-style `en1`. The only realistic path was therefore to
create a virtual `awdl0` inside the driver and let the native stack (IPv6/TCP/TLS/mDNSResponder)
carry AirDrop.

## Milestone 6 — data plane attempt (2026-11-07 20:39)

New capabilities: `kMtkUcTxData` (raw data frame TX), A-MSDU capture, `awdlbridge` (utun ↔ AWDL
data frames, multicast scheduled in slots 0/10), `awdlble` (AirDrop BLE advertisement), `blescan`.

Field-by-field comparison with the iPhone's MIF led to fixes: Version TLV `0xa0`, election metric
`515/0` → follow the master (`515+1`), data-path TLV 12 (47 bytes, flags `0xbf23`) reused verbatim
with our MAC, unknown TLV 17 reused.

**Wall**: even with frames nearly identical to a genuine one (TLVs 4/5/12/17/18/24 all copied from
Apple frames), the iPhone sent **no AWDL data frames at all** (20 s dump: 7 MIFs, 0 data frames) and
`ping6` lost 100 %. The phone does not consider us a valid AWDL peer, or its AirDrop server (mDNS)
never started — §2 of the spec says a receiver needs a BLE trigger first.

## Milestone 7 — BLE trigger layer (2026-11-07 21:01)

`awdlble` (CoreBluetooth) reported success with tag `aa bb cc`, but `blescan` **never** saw
`4c 00 05 12 40 aa bb cc`; only Apple system advertisements (`4c 00 0c`, `4c 00 10`, …) appeared.

**Conclusion: macOS CoreBluetooth does not let a third-party app emit custom manufacturer data.**
The BLE trigger path is closed from user space.

Final state of that phase: driver monitoring/channel/mgmt+data TX/RX ✅, AWDL frame construction ✅,
clock sync and hopping ✅, utun bridging ✅, **BLE AirDrop advertisement ❌ (macOS restriction)**,
iPhone mDNS server ❌ (never triggered).

---

## Milestone 8 — `awdl0` appears (2026-11-07 23:50, switch to the upstream driver)

Abandoned the self-built IO80211 skeleton (stuck on OCAK vtable patching and missing private types)
and switched to <https://github.com/thanhnndev/mt7925-macos> branch `dev/awdl`
(BSD-3-Clause-Clear).

Build/install: `bash scripts/build.sh` (CLT only, pulls MacKernelSDK), four kexts; our RZ717 is
`14c3:0717` so `0x071714c3` had to be added to `Info.plist`; boot args
`itlmt_native=1 itlmt_native_if=1 itlmt_native_dp=1 itlmt_fast=6 itlmt_awdl=1`;
`install_upstream_awdl.sh` / `restore_upstream_awdl.sh` for install and rollback.

Result:

```
awdl0: flags=8843 … inet6 fe80::5c28:faff:fe1c:2847%awdl0
en2: Third-Party Wireless Card | itlmt7925Native 0.1
Finder > AirDrop: the iPhone appears under "My Devices"; receiving can be set to Everyone
```

Open: the iPhone does not see the Mac (either the Mac never advertises `_airdrop._tcp` on awdl0 or
the data plane is not up); native Wi-Fi scan returns nothing on 25G332 (ioctl cmd=10 accepted, no
results). ABI check: `skywalk-if.diff` is identical on 25G241 and 25G332.

## Milestone 9 — Bluetooth fixed, listen windows (2026-11-08 01:16)

Bluetooth used to report `MtkBtState=error`, `half-loaded, subsys reset`, chipset `BCM_4350C2`.
Cause: upstream `MtkBtFw`'s `Info.plist` matched `idVendor=0x0489 idProduct=0xE11E` (their test
hardware); our device is **`0x0E8D:0x0717`**. After fixing the IDs:

```
com.itlmt7925.mtkbtfw (0.1.0) loaded
Address: 14:5A:41:0C:B5:10  State: On  Chipset: THIRD_PARTY_DONGLE  FW: v9736 c4881
```

(branch `fix/bt-download-retry`, sources in `mt7925-btfix/`; `MtkBt.kext` and
`SimpleMtkBtFw.kext` disabled.)

AWDL listen windows (`itlmt7925_awdl.cpp`): `awdlLsnArm()` was only called when a peer existed, so
without a peer nothing listened and `rx` stayed 0. Three patches: a no-peer fallback that rotates
pure listen windows over 6/44/149; removing the `awdlListenPeer(now)` condition so PSF always goes
out in a listen window; a 70 ms rate limit on the fallback window (160 windows/s left no dwell).
Result `lsn=17210/539/0 ch=149` (windows running) but still `rx=0`.

## Milestone 10 — Wi-Fi + Bluetooth + AWDL all working (2026-11-08 01:43)

**The hidden switch is `itlmt_m1=0`**: M1 (the firmware init thread) only starts when
`PE_parse_boot_argn("itlmt_m1", …)` succeeds. Without it M1/M2/M3 never run, `wifi-state=off`
and native scans return `kIOReturnNotReady`. With `itlmt_m1=0` firmware init completes and scan
and association work.

State at that cold boot:

```
Bluetooth: 14:5A:41:0C:B5:10 · On · THIRD_PARTY_DONGLE · FW v9736 c4881
Wi-Fi:     wifi-state = "connected" (en2 associated)
awdl0:     fe80::9c3a…
AWDL:      peer4=690 rx=690 ptl=110 dtx=36 drx=40 last=6ac5f55cce04 (the iPhone)
```

Verified: the Mac keeps receiving the iPhone's AWDL frames (−70 dBm), AWDL data flows both ways
(dtx/drx grow), the Mac advertises `_airdrop._tcp` on awdl0 (instance `51390b876287`), Finder sees
the iPhone. Open: the iPhone does not see the Mac — the same stage upstream describes as
"the iPhone's unicast arrives; ours mostly does not".

## Milestone 11 — where iPhone discovery / sending is blocked (2026-11-08 02:30)

1. **No LE advertisement on air** (prerequisite for the iPhone to discover the Mac): the phone's
   Bluetooth settings show the Mac (classic discoverable advertising works), but nRF Connect sees
   no LE advertising at all, while CoreBluetooth reports `isAdvertising=true`. A corrected private
   API call (`i28@0:8C16*20`, second argument `char *`) returns 0x0 and still nothing appears.
2. **Mac → iPhone sending hangs on "Waiting"**: dragging a file in Finder waits forever — the same
   stage as upstream's "the Ask never arrives". A privately circulated Rust project's fix note
   claims iOS 26/27 appends two bytes (e.g. `20 e4`) to the TLV4 channel table and that a parser
   requiring zero padding rejects the whole frame; `thanhnndev`'s C++ implementation already builds
   73 bytes and its `awdlTlv4Timeline` does not check that tail, so this is not the cause here.

## Milestone 12 — final verdict on LE (2026-11-08 02:55)

* LE **scanning works**: 10 s central scan, state=5, 27 devices including the iPhone.
* LE **advertising never reaches the air**: `startAdvertising` → `isAdvertising=true` for 50 s,
  the iPhone's nRF Connect sees nothing.
* Private LE capability APIs return 0, but the same API family reads the BD address correctly,
  so those failures are not proof of missing LE support.
* Classic Bluetooth discoverable advertising works.

=> Under this firmware/driver pair the MT7925 **can receive LE but cannot transmit it**, which is the
insurmountable blocker for the AirDrop BLE beacon.

## Milestone 13 — every LE advertising path exhausted (2026-11-08 03:00)

| Path | Result |
|---|---|
| CoreBluetooth ADV_IND (name `MtkTest`, 50 s) | `isAdvertising=true`, invisible to the iPhone |
| Private API ADV_IND (fixed signature) | 0x0/0x0/0x0, invisible |
| Private API ADV_NONCONN_IND (type 3, 25 s) | 0x0/0x0/0x0, invisible |
| Raw HCI (`hciadv`) | API removed in macOS 26 |

Firmware file: only `BT_RAM_CODE_MT7925_1_1_hdr.bin` (448,367 bytes, same name as the Linux mt76
one); no alternative variant available.

## Milestone 14 — where the Bluetooth HCI layer really breaks (2026-11-08 03:45)

USB layout (printed by `MtkBtFw`):

```
interface 0 alt 0, 3 endpoints
  0x81   interrupt IN   mps 16    HCI events
  0x82   bulk IN        mps 512   ACL in
  0x02   bulk OUT       mps 512   HCI commands / ACL out (no interrupt OUT)
```

`UsbExclusiveOwner = "pid 822, bluetoothd"` — bluetoothd owns the device in user space; our driver
can only load firmware early and must let go afterwards.

Driver-side HCI probes (eight iterations): commands on the control endpoint time out; commands on
bulk OUT 0x02 (including an initial HCI Reset) produce no event; interrupt IN 0x81 (length ≤ 16,
DMA prepare, clearStall, re-arm after abort) still ends in `kIOReturnAborted` because bluetoothd
already owns the device.

macOS fails the same way (`bt-reinit.log`):

```
OI_HCICMD_ReadLocalVersionInformation failed: STATUS 408
LE_InitMultipleAdvertising failed with status STATUS 408
LeBroadcaster.mm::stackDidStart() -- status != OI_OK -- 0x0198
```

## Milestone 15 — Bluetooth transport architecture, final (2026-11-08 04:10)

There is **no kernel USB Bluetooth transport class**: `IOBluetoothHCIController`'s provider is
IOResources, there are no `IOBluetoothHostController` / `IOBluetoothUSBTransport` / `HCIUSB`
symbols, and the only child is `IOBluetoothHCIUserClient`, through which bluetoothd injects HCI.

bluetoothd contains the USB transport (`FindTransportType -- USB Transport`, `IOUSBHostDevice`).

Consequences: a self-written HCI transport kext has no provider protocol to attach to; intercepting
bluetoothd's USB access is impossible because it owns the device exclusively. The only practical
route is to give the BLE beacon to a Bluetooth adapter macOS already supports (BCM20702/20703),
while AWDL keeps running on the MT7925.

---

## Milestone 16 — self-built LE extended advertising works (2026-11-08 09:00–11:30)

**Breakthrough: bypass the macOS Bluetooth stack and let the driver transmit the AirDrop BLE
beacon directly; the iPhone's scanner sees it.**

Findings, all obtained with a self-written HCI probe:

1. Command channel: the **control endpoint (class request 0x20) without the HCI packet-type byte**.
   With the type byte the controller answers `0f 04 01 01 01 xx` (misaligned); without it the
   reply is the correct `0e 04 01 <op> <op> 00`.
2. Event channel: interrupt IN `0x81` with a **maximum packet size of 16 bytes** (reading 1024
   returns `kIOReturnAborted` immediately).
3. Controller: HCI 5.4, MediaTek `0x0046`, LE extended advertising with **253 advertising sets**.
4. **Every legacy advertising command returns `0x0C` (Command Disallowed)**; every extended one
   succeeds: `LE Set Extended Advertising Parameters` (0x2036), `… Data` (0x2037) and `… Enable`
   (0x2039), all status 0.
5. Parameters: `advertising_event_properties = 0x0013` (legacy PDU, connectable, scannable),
   100 ms interval, `tx_power = 0x00`, channel map `0x07`.
6. Advertisement, 30 bytes (limit 31):

```
02 01 06                                            Flags
17 ff 4c 00 05 12 40 <tag3> 00000000 03 00×8 00     AirDrop TLV (BLE-2)
02 09 4d                                            local name "M"
```

7. The iPhone's Bluetooth debugging app sees `M` at −65 dBm.

Side fix: the AWDL TX ring (`kAwdlTxSlots` 16 → 64) removed all ring-full drops (`dtx full`
192 → 0).

New conflict: the iPhone's share sheet needs the Mac's BLE beacon (only our driver can send it,
and that requires owning the controller), while it also needs the Mac's `_airdrop._tcp` AWDL
service (only macOS's sharingd publishes it, and only while Bluetooth is available). Measured:
while the driver owns the controller, restarting sharingd registers the service for a moment at
best. The remaining path was therefore to publish the service ourselves on awdl0 and implement the
AirDrop receiver.

## Milestone 17 — self-built receiver and real iPhone interaction (2026-11-08 13:00–13:40)

Evidence straight from the driver's `m3-log`:

1. The iPhone sends mDNS queries to us over AWDL:

```
awdl data rx len=294 ch=6 fc=0800 da=3333000000fb sa=8a7f5ede444b et=86dd
```

`da=33:33:00:00:00:fb` is IPv6 mDNS multicast.

2. The iPhone connected to our own AirDrop service:

```
accept from fe80::887f:5eff:fede:444b     (= EUI-64 of 8a:7f:5e:de:44:4b)
tls handshake failed: Connection reset by peer
```

3. The pieces were complete: `awdl-svc/awdlsvc.c` registers `_airdrop._tcp` with
   `DNSServiceRegister` + `kDNSServiceFlagsIncludeAWDL` (visible on awdl0, interface 10);
   `awdl-svc/airdrop_rx.py` is a TLS server with `/Discover`, `/Ask`, `/Upload` (self-signed
   certificate, verified locally); the captured discovery request carries `SenderRecordData`
   (3,718-byte PKCS#7) and `DeviceSupportFlags` 111611.

## Milestone 18 — TLS works, stops after `/Discover` (2026-11-08 13:40–13:50)

```
13:44:47 tls TLSv1.2 ECDHE-RSA-AES256-GCM-SHA384
13:44:47 TLS ok from fe80::1029:e1ff:fefa:9804
13:44:47 POST /Discover body=3834      (DeviceSupportFlags + SenderRecordData)
13:44:47 Discover response: 169 bytes  (ReceiverComputerName/ModelName/MediaCapabilities)
13:45:17 Connection reset by peer      (peer resets 30 s later)
```

So the self-signed certificate is accepted, TLS negotiates, and `/Discover` arrives; the remaining
problem is that our `/Discover` response is not taken up (no `/Ask` follows).

Other side: browsing AWDL locally only shows our own service, never the iPhone's
`_airdrop._tcp`; Finder lists the iPhone (via BLE) but dragging a file leaves AWDL unicast at
`uc=0/0` — macOS never starts an AWDL data connection. Multicast TX `grp=22/76` (≈29 %).

## Milestone 19 — first complete AirDrop reception (2026-11-08 14:35–14:55)

MacBook Pro 2019 (macOS 12.4) → this machine: `/Discover` → `/Ask` → `/Upload` all completed and a
238,657-byte PNG (1178×678) arrived, unpacked from gzip + CPIO (odc) and verified intact.

Working combination (three components at once):

1. `MtkBtFw.kext`: loads the firmware, transmits connectable LE extended advertising (Flags +
   AirDrop TLV + name `M`) and **owns the controller for one hour** (`bt-state=loading`, macOS
   Bluetooth off);
2. `awdlsvc advertise-only`: `DNSServiceRegister` + `kDNSServiceFlagsIncludeAWDL` for
   `_airdrop._tcp` on port 8770;
3. `airdrop_rx.py`: TLS 1.2 self-signed + `/Discover` `/Ask` `/Upload` (`Expect: 100-continue`,
   chunked and `Content-Length` bodies; unpack gzip+CPIO into `~/Downloads`).

Measurements:

| Item | Result |
|---|---|
| TLS version / cipher | TLSv1.2 ECDHE-RSA-AES256-GCM-SHA384 |
| `/Ask` at fixed 6 Mb/s | 139 s — exceeds the sender's 45 s timeout, fails |
| `/Ask` at 12 Mb/s | 3–11 s |
| Full transfer (230 KB) | 88 s, succeeded once |
| AWDL multicast TX | 27 ok / 25 fail (≈52 %) |
| AWDL unicast TX | 2 ok / 26 fail (≈7 %) ← the stability bottleneck |

## Milestone 20 — stability experiments (2026-11-08 14:45–15:15)

Raising the fixed AWDL data rate from OFDM 6 Mb/s to 12 Mb/s (TXD rate index 20) cut a 103 KB
`/Ask` from 139 s to 2–11 s and a 7 KB `/Upload` to about a second. A second full reception
succeeded (`/Ask` 18,477 B in 2 s, `/Upload` 7,223 B in 1 s, PNG 7,010 B extracted).

The station-record experiment was ruled out: sending `STA_REC_UPDATE` for the AWDL peer
(`CONNECTION_IBSS_ADHOC = 0x40004`, wcid 20, `muar_idx = 1`) is accepted by the firmware (`rc=0`)
but unicast then succeeds *zero* times (`uc=0/56`), worse than the AP wcid. Correcting the PHY rate
set for the channel did not help either (`uc=0/6`). So the missing ACKs are not about the station
record; `awdlStaOn` was left off and the AP wcid path restored.

## Milestone 21 — IBSS BSS experiment: the real reason for missing ACKs (2026-11-08 15:30–15:45)

Adding a full BSS context — `BSS_INFO_UPDATE` with `bss_idx=1`, `CONNECTION_IBSS_ADHOC`,
`omac_idx=1`, plus `STA_REC_UPDATE` for the peer (wcid 20) — is accepted by the firmware (`rc=0`)
and unicast still succeeds zero times (`uc=0/15`). Per-frame status:

```
txfree awdl pid=4 st=1 tries=15 ch=149 win=1
```

i.e. every frame retried the full 15 times, always on the same channel, never acknowledged.
Additionally:

```
awdl sync: roc fail ch=44 grant=0 status=255 after 309 ms
```

The driver cannot start a ROC on channel 44 (status 255, regulatory/DFS), while the peer hops
between 6/44/149 and we can only cover 6/149/157.

Conclusion: the unicast failures come from **AWDL slot/channel scheduling misalignment**, not from
the station record and not from the rate — upstream's core difficulty.

## Milestone 22 — window timing fixed (2026-11-08 20:20–21:25)

| Metric | Before | After |
|---|---|---|
| `avglate` (mean lateness) | 43–53 ms | **1.6–1.75 ms** |
| `late` (late windows) | 541–1300 | **11–69** |
| `dwell` (actual dwell) | squeezed to 1 ms | **98.9 ms** |
| `rolled` (realigned windows) | 0 | **95–281** |

Root-cause chain, found in three steps:

1. The `startUs` handed over by the Family is often already in the past, so
   `setupAt = startUs − lead` is past too, the wait is skipped, and after ~38 ms of setup the
   window is 40 ms+ late;
2. the condition must be `now + leadUs > startUs` ("can the setup still begin in time?"), not
   `startUs < now`;
3. **`awdlAwPeriodUs` is the AW length (16 TU = 16,384 µs), not the AWDL cycle** — rolling must use
   102,400 µs (100 TU). The old 20,000 µs threshold rejected 16,384, so `rolled` stayed 0 forever.

State afterwards: management frames `tx=399/400` (99.8 %), peer action frames `af=682`, but
discovery and transfers remained intermittent. Remaining blocker: the AWDL trees do not merge —
`role=1`, `master`/`top` are ourselves while the peer is its own root. The driver side does
everything it can (AW-end message `0x2b` is posted, action frames reach the peer manager, the
election TLV is parsed into `itlmt-awdl-peer-el`), but the election itself lives in the Family
(`runElection` in wifip2pd) and is only reported back through `setAWDL_SYNC_STATE`.

Environment note: when the peer and the Mac sit on different Wi-Fi bands (2.4 vs 5 GHz) AWDL sync
degrades noticeably; the device only appeared after both were on the same band.

## Milestone 23 — full AirDrop transfer, caused by a TXT flag advertising an unimplemented feature (2026-11-08 23:00 → 11-09 01:23)

Result: MacBook Pro 2019 / macOS 12.4 → this machine, a 7,010-byte PNG landed in `~/Downloads`,
`/Discover` → `/Ask` → `/Upload` in about one second (`PNG image data, 136 x 72, 8-bit/color RGBA`,
gzip intact, inner cpio `070707`).

Root cause: `awdlsvc.c` registered `_airdrop._tcp` with `flags=141` (`0x08d` = URLSharing |
**Pipelining** | MixedTypes | DiscoverMaybe) while the receiver never implemented UP-8 (buffering
the body during the prompt). It advertised a capability it did not have, which MDNS-5 forbids.

Failure fingerprint:

```
POST /Upload … totalbytes: 7010
  chunkhdr#1 b'A'        ← 0x0A, exactly 10 bytes: gzip header 1f 8b 08 00 00 00 00 00 00 03
  chunkhdr#2 b''         ← EOF one second later, no 0\r\n terminator
gzip failed: Compressed file ended before the end-of-stream marker
```

The Mac showed "copy cancelled". Changing our responses made no difference (early 200, `100
Continue`, or no response at all all died the same way), and per-second `raw rx` never moved, so
those 7,010 bytes never reached the air — the sender refused to send, it was not packet loss.

Fix: `flags=141 → 137` (`0x089`, MDNS-5's recommendation for a minimal receiver). Without
Pipelining, UP-3 applies: the sender adds `Expect: 100-continue` (a header that never appeared
before — that is the fingerprint), we answer `100 Continue`, and the body arrives in one 7,265-byte
piece.

Timeline of the bug (three hours of misdiagnosis): 14:39 milestone 19 succeeded with 238,657 bytes;
16:11 `awdlsvc` was changed to `flags=141` with the comment "let the sender pipeline the upload";
23:00–01:23 every `/Upload` died at 10 bytes and was misread as an AWDL unicast problem.

Protocol details confirmed along the way: macOS 12.4 uses the **legacy** `/Ask` (no `TransferID`,
`TransferType` or `FileSize`, so an empty `id:` is normal); `/Ask` must be answered **after** the
body is read (an early 200 upsets CFNetwork's state machine); `/Upload` follows UP-3 — send
`100 Continue` only when `Expect` is present, and answer `200` + `Content-Length: 0` +
`Connection: close` once the body is complete.

## Milestone 24 — the wall for sustained transfers: collapses between 48 and 131 KB (2026-11-09 01:25–01:31)

Same link: small files go through instantly, a 238,657-byte image stopped mid-way three times, at a
different point each time.

| Time | Peer behaviour | Where it stopped |
|---|---|---|
| 01:25 | `/Ask` body 103,892 B at 13,037 B/s, then `/Upload` `chunkhdr#1 b'20000'` (= 131,072) | zero payload bytes, silent for 105 s |
| 01:29 | `/Ask` body 103,892 B at 5,057–15,237 B/s | silent after 49,152 / 103,892 |
| 00:50 | `/Ask` body 103,892 B | silent after 65,536 / 103,892 |

During the stall (10 s window): `awdl0 Ibytes +0` (not one byte reached the network stack), driver
`rx +61` (frames did arrive over the air), `syncs +4` (the peer's sync frames continued, so layer 2
was alive), TCP out-of-order 0 (no retransmissions visible to the stack).

Two explanations remain, and only driver counters can separate them: the peer stops sending because
its AWDL session considered us unusable (the device disappears from its list right after a
failure), or we drop frames between radio and stack (`rx` grows while `awdl0` stays at zero).

Prepared experiment: the core RX path logs `awdl drx path: candidate/badsnap/parsed/queued/ringfull`
and the native side keeps `drx=in/up/notus/fail/dup`. Read both before and after a stall:
`candidate` flat means no matching frames reached the host; `candidate` up but `parsed` flat points
at `badsnap`; `parsed` up but `queued` flat points at `ringfull`; `queued` up but native `in` flat
points at the core→native queue; native `in` up but `up` flat points at `notus/fail/dup`. These
counters only cover frames that reached the driver while a ROC was active.

This is the same wall as upstream's "5 GHz unicast fails about 71 %".

Receiver-side improvements kept: chunked reads in 8 KB slices with progress every 32 KB and a
`chunk#N EOF at X/Y` line on a mid-body disconnect; body-phase timeout 25 s (120 s between
requests) — note that a 25 s timeout closes the connection and the Mac then reports "your request
was rejected", so the timeout must exceed the sender's own patience if that is what you measure.

---

## 2026-11-09 — Bluetooth probe mode note

`MtkBtFw`'s `hciProbe()` deliberately holds the USB interface for 21,600 s to transmit and scan
AirDrop BLE beacons; `bt-state=loading` with macOS Bluetooth off is therefore the expected state of
probe mode. An attempt to hand the interface back to the system right after firmware load (plus a
`BtLeDlFix` update) would have cancelled the probe capability in use; after the user pointed this
out, the two drivers were restored from the `20261109-193459` backups with matching SHA-256 and the
sources reverted. That attempt must not be recorded as a verified Bluetooth fix.

## 2026-11-09 — discovery intermittency traced to AWDL role oscillation

The BLE probe driver was starting the beacon (`LESetExtAdvEnable status=0`) and `awdlsvc`,
`airdrop_rx.py` and the LaunchAgent were running; the MacBook's `/Discover` had arrived, so
discovery had worked at least once. Sampling `itlmt-awdl` every 2 s showed `role=2
master=e69e053cb192` and `role=1 master=<self>` alternating about every 4 s with `chg` rising from
86 to 92, while `peer4` and `syncs` kept growing. `setAWDL_SYNC_STATE()` accepted the Family's
self-master result unconditionally, overriding the driver-side follow decision made by
`awdlElectionConsider()`; the next peer frame switched back — hence the oscillation.

Fixed in `itlmt7925Native`: while following a peer whose election frames arrived within the last
8 s, ignore the Family's self-master override; allow it again once the peer is lost. Added an
`fself` counter. After the reboot the MacBook could see the device, `role=2` stayed put for 20 s,
`chg=1` did not grow, `fself=22→27`, `peer4=284→348`, and `airdrop_rx.py` answered TLS
`/Discover` at 19:44:17, 19:44:34 and 19:45:12. No `/Ask` or `/Upload` occurred, so a full transfer
was still unverified; `awdl txfree` unicast remained `uc=0/4 ucheard=0/70`.

## 2026-11-09 — exploring the macOS 26 system Bluetooth path (not switched)

At 19:47 the MacBook lost the device again while the driver still reported `role=2`, `chg=1`,
`fself=59`, `peer4=754`, with the service processes alive and a `/Discover` in flight, so role
oscillation was already fixed and the disappearance has another cause; core unicast TX was still
`uc=0/5 ucheard=0/72`, so the later instability cannot be blamed on Bluetooth alone.

Correction to the earlier "a workable path was found": the system Bluetooth takeover is neither new
nor proven to complete AirDrop stably on 25G332. Milestones 11–15 record that with `State: On`
CoreBluetooth reports advertising success while nothing reaches the air, and that `bluetoothd`
reports `ReadLocalVersionInformation STATUS 408` and multi-advertising init failures while owning
the USB device — which is why the driver-side BLE beacon was chosen. Separately, on 25G241,
`BtLeDlFix 1.2.0` did make the system LE advertisement reach the air (a BLE mouse worked), but a
stable AirDrop transfer was never verified. Both records must be read together: a controller that
reports On, or a patch whose bytes match, is not an AirDrop fix.

A controlled retest of the system path would be: `MtkBtFw` loads firmware, closes the USB interface
and hands it to `bluetoothd`; `BlueToolFixup` handles the third-party controller; `BtLeDlFix`
patches the LE data-length wait and extended advertising. The machine runs macOS 26.7.2 / 25G332
while `BtLeDlFix 1.2.0`'s third advertising patch only matched 25G241; disassembling
`/usr/sbin/bluetoothd` (x86_64) showed the call displacement moved from `3e 6b ce ff` to
`1e 6c ce ff`. A 25G332-specific match was added and all four required patterns are unique and
page-local in the current binary — that proves static matching only, not runtime HCI or air
behaviour.

`MtkBtFw` gained `mtkbt_system=1`: with it, the USB interface is handed back after firmware init;
without it the six-hour BLE beacon/scan probe stays. A system-mode test needs its own boot, and the
self-built `awdlsvc` registration should be stopped once the system AirDrop service is confirmed, so
two services of the same type do not confuse peers. Verification order: `bt-state=ready`, a real
address with State On, `bluetoothd` extended advertising, MacBook/iPhone discovery and `/Discover`,
and only then a transfer. If system mode fails, dropping the boot argument returns to probe mode
and the old EFI drivers are still backed up.

Follow-up checks recorded the same day: after the 19:54 reboot the system Bluetooth still had no
controller (`system_profiler` reported `controllerInfo == nil`, `bluetoothd` restarting with exit
code 1), the live logs showed `External Bluetooth dongle was not found -- setting chipset to
default BCM_4350C2`, `HCI Transport is set to H4BC`, then a 15 s wait for a non-existent
`/dev/cu.BLTH` and `Transport layer initialization failed`, whereas the historical success log
`bt-le-clean.log` shows `Found_USB_Dongle`, `FoundBluetoothInterfaces(...) returned true`,
`THIRD_PARTY_DONGLE` / `USB` and bluetoothd taking exclusive ownership. Disabling `BtLeDlFix` in an
A/B boot did not restore the system Bluetooth either, so it is not the sole cause. NVRAM
`bluetoothExternalDongleFailed` reads `01` and reverts to `01` shortly after being cleared, which
may be a consequence of repeated init failures rather than a cause.

The next driver experiment (`MtkBtFw 0.1.2`) notices that `start()` returns immediately while a
background thread loads firmware, whereas `bluetoothd` calls `IOKitWaitQuiet()` and then looks for a
USB controller. In `mtkbt_system=1` mode the driver now wraps the background phase in
`IOService::adjustBusy(1/-1)` so that wait covers it; probe mode does not hold busyState. The
implementation was re-reviewed (busy taken before `registerService()`, released by the worker and on
the `startThread` failure path) and the build is 501,616 bytes with `CFBundleVersion 0.1.2`. This is
exactly the race the 20:24 log describes; if the log still falls to H4BC the hypothesis is refuted.

Alongside it, a genuine firmware-level bug was found and fixed in the core: `m3SnifferConfig()`
wrote `fastBw` (`CMD_CBW_*`: 0=20, 1=40, 2=80, 3=160) into the sniffer's `bw` byte, while the
firmware expects its own `ch_width` table (20/40→0, 80→1, 160→2, 80+80→3). On a 160 MHz AP the
firmware was told "80+80 with center2 = 0" and stopped answering MCU commands (`sniffer cfg
rc=-110`, `firmware not answering MCU commands`, firmware reload) — the same bug upstream fixed in
§10.84. The fix maps through `kSnifferBw[4] = {0, 0, 1, 2}`; off-channel windows stay at 20 MHz.
This also gives the repeated "chip unresponsive, needs a full power cycle" episodes
(`uhw 70028610 read failed`, `chip id read failed`) a second plausible source on the Wi-Fi side;
that is not yet verified.

Verification order after the next boot, one step at a time: (1) Bluetooth — `bluetoothd`
`runs`/exit code, a real address in `system_profiler`, `UsbExclusiveOwner=bluetoothd`, and whether
the log moves from H4BC to `HCI Transport is set to USB`; (2) firmware — no `sniffer cfg rc=-110`
and no `firmware not answering MCU commands`; (3) only when both are clean, return to the AirDrop
chain (`role`, `syncs`, unicast `uc=`).
