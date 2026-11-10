# mt7925-awdl — AWDL and AirDrop on MediaTek MT7925 under macOS

Apple Wireless Direct Link (AWDL) and AirDrop for a Hackintosh whose Wi-Fi is a
**MediaTek MT7925** (PCI `14c3:0717`, Bluetooth USB `0E8D:0717`) — a chip macOS has
no working AWDL support for.

**Status: receiving works.** A real Mac (macOS 12) and an iPhone both discover
the machine and a macOS sender completes `/Discover` → `/Ask` → `/Upload`.
Several files were received end to end (238 KB and 7 KB PNGs, verified intact).
Transfer stability is still limited — see [Limitations](#limitations).

The full engineering log (24 milestones, with the counters behind every claim) is
in **[docs/FINDINGS.md](docs/FINDINGS.md)** — English; the original Chinese working
log is [docs/FINDINGS.zh.md](docs/FINDINGS.zh.md).

## Results at a glance

| Capability | Result |
|---|---|
| LE extended advertising straight from the driver (macOS's stack cannot) | ✅ the iPhone's scanner sees the AirDrop beacon `M` at −65 dBm |
| Self-hosted `_airdrop._tcp` on `awdl0` | ✅ advertised on interface 10 while macOS Bluetooth is off |
| TLS plus `/Discover` `/Ask` `/Upload` | ✅ TLS 1.2, ECDHE-RSA-AES256-GCM-SHA384 |
| **Complete AirDrop reception** | ✅ 238,657-byte and 7,010-byte PNGs, both verified intact |
| AWDL tree merge (become a slave of a real Mac) | ✅ driver-side election follow, `role=2` held |
| Sustained transfers above ~100 KB | ❌ collapse mid-way — see [Limitations](#limitations) |

What the fixes changed, measured:

| Metric | Before | After |
|---|---|---|
| AWDL window lateness (`avglate`) | 43–53 ms | **1.6–1.75 ms** |
| Late windows (`late`) | 541–1300 | **11–69** |
| Actual window dwell (`dwell`) | squeezed to 1 ms | **98.9 ms** |
| AWDL TX ring drops (`dtx full`) | 192 | **0** |
| `/Ask` of 103 KB | 139 s — sender times out | **2–11 s** |
| AWDL unicast acknowledged | ~7 % | ~7 % — unchanged, this is the wall |

The wall is not in this repository: unicast ACKs depend on the AWDL window schedule that
macOS's own `wifip2pd` hands to the driver (see [Limitations](#limitations) and milestone 22
in the notes).

---

## Why this is unusual

macOS ships AWDL in `wifip2pd`, but it only runs when the Bluetooth stack can
advertise a Low Energy beacon: Apple devices find AirDrop receivers through a
BLE advertisement, then browse AWDL. On the MT7925 macOS's Bluetooth stack
never starts its broadcaster:

* it sends HCI commands on the standard bulk endpoint, while this controller
  only answers on the **control endpoint, without the HCI packet-type byte**;
* it relies on Broadcom vendor commands (`BD_VSC_LE_META_*`, `BD_VSC_OLYMPIC_*`)
  that the MediaTek firmware rejects (`not supported`, status 701).

So `LE_InitMultipleAdvertising` fails, `LeBroadcaster` never starts, and the
Mac is invisible to AirDrop.

This project works around that in three pieces that run together:

| Piece | What it does |
|---|---|
| `bt-beacon/` (kext patch) | Loads the BT firmware, then programs LE **extended** advertising directly (the legacy commands are refused with `0x0C Command Disallowed`) and holds the USB interface so nothing else touches it. The phone sees a valid AirDrop beacon. |
| `receiver/awdlsvc` | Registers a `_airdrop._tcp` service on `awdl0` with `kDNSServiceFlagsIncludeAWDL`, so the AWDL side is advertised even while macOS's own Bluetooth is off. |
| `receiver/airdrop_rx.py` | A TLS server speaking AirDrop's HTTP: `/Discover`, `/Ask`, `/Upload`, `Expect: 100-continue`, chunked bodies; unpacks the gzip/CPIO archive into `~/Downloads`. |

The driver patch (`driver/`) keeps AWDL healthy underneath: it enlarges the
AWDL TX ring (16 → 64 slots, dropped frames 192 → 0) and raises the fixed data
rate to 12 Mb/s, which cuts a 103 KB `/Ask` from 139 s (sender timeout) to 2–11 s.

---

## Requirements

* macOS 26 (built against 25G332) on a Hackintosh with an MT7925;
* the upstream driver [`thanhnndev/mt7925-macos`](https://github.com/thanhnndev/mt7925-macos)
  branch `dev/awdl` (the native Wi-Fi front end must already work: `awdl0` up,
  Wi-Fi associated);
* Python 3 (system one is fine), `clang`, and command line tools.

## Install

1. Build the two kexts with the patches in `driver/` and `bt-beacon/`
   (each directory's README explains where the patch goes).
2. Copy them to `EFI/OC/Kexts/` on the ESP, next to the upstream kexts.
3. Boot with the upstream boot arguments (`itlmt_native=1 itlmt_awdl=1 …`).
4. After login:

```sh
cd receiver
clang -O2 -o awdlsvc awdlsvc.c
./awdlsvc advertise-only &            # advertises _airdrop._tcp on awdl0
python3 airdrop_rx.py &               # TLS + /Discover /Ask /Upload on :8770
```

5. On the sender: AirDrop → **Everyone**, open Finder ▸ AirDrop (or the iOS
   share sheet), pick the machine, send a file. It lands in `~/Downloads`;
   the raw archives stay in `~/Downloads/AirDropInbox`.

The Bluetooth beacon is owned by the kext and runs for one hour after boot
(`IOSleep(3600000)`); while it holds the controller, macOS reports Bluetooth
as off. That is expected: the beacon and the service advertisement come from
our two components instead.

---

## Evidence

Bytes on air (iPhone sees the beacon):

```
02 01 06                                              Flags
17 ff 4c 00 05 12 40 <tag3> 00000000 03 <8 bytes> 00   AirDrop TLV (Apple 0x004C)
02 09 4d                                              "M"
```

A full receive, as logged by the receiver:

```
TLS ok from fe80::607c:46ff:fee8:f076        TLSv1.2 ECDHE-RSA-AES256-GCM-SHA384
POST /Discover body=3790
POST /Ask body=18477                         from "cao的MacBook Pro", 截屏….png
POST /Upload body=7223                       100 Continue sent
extracted ~/Downloads/截屏….png 7010 bytes
upload complete: 1 file(s)
```

## Limitations

* **Unicast AWDL TX is unreliable (~7 % ACKed).** The driver transmits each
  data frame up to 15 times (`txfree awdl st=1 tries=15`) and the peer rarely
  acknowledges. Providing an IBSS `BSS_INFO_UPDATE` and a peer `STA_REC`
  (both accepted, `rc=0`) did not change it, nor did 6 / 9 / 12 Mb/s. The
  remaining suspect is the AWDL window schedule: our transmissions do not land
  in the peer's availability window, and channel 44 cannot be used at all
  (`awdl sync: roc fail ch=44 status=255`).
* Consequence: small transfers (a few KB) usually finish, large ones stall or
  fail mid-way.
* The iOS sender stops after `/Discover` (macOS senders continue).

These are window-scheduling problems in the shared core driver — upstream's
own frontier — not application bugs.

## License

The patches are derivative works of `thanhnndev/mt7925-macos`
(BSD-3-Clause-Clear). The receiver and the beacon tool are provided under the
same terms.
