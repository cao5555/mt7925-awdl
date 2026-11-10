# Core driver patch (`itlmt7925`)

Apply on top of [`thanhnndev/mt7925-macos`](https://github.com/thanhnndev/mt7925-macos),
branch `dev/awdl` (tested at `95c9a84`).

```sh
cd mt7925-macos
git checkout dev/awdl
git apply /path/to/itlmt7925-awdl.patch
bash scripts/build.sh itlmt7925 itlmt7925Native
```

What the patch changes:

* `itlmt7925.hpp`, `itlmt7925_m3.cpp`
  * `kAwdlTxSlots` 16 → 64: the AWDL data ring no longer fills up
    (`awdl txfree … full=192` before, `0` after).
  * A `rate` field in `M3TxOpt`; AWDL data frames go out at OFDM 12 Mb/s
    instead of 6 Mb/s (`/Ask` 103 KB: 139 s → 2–11 s).
  * `m3AwdlBssAdd()` / `m3AwdlPeerAdd()`: an IBSS `BSS_INFO_UPDATE` for the
    AWDL link plus a `STA_REC_UPDATE` for the peer (`CONNECTION_IBSS_ADHOC`,
    wcid 20). Both are accepted by the firmware (`rc=0`) but do **not** fix
    unicast ACKs, so they are off by default (`awdlStaOn = false`); enable with
    boot arg `itl_awdlsta=1` if you want to experiment.
* `Info.plist`: adds this card's PCI ids (`0x792514c3 0x071714c3`) so the kext
  claims the device.
* `itlmt7925Native/itlmt7925_awdl.cpp`: no-peer fallback in `awdlLsnArm()`
  (rotating 6/44/149), `awdlSyncVer >= 4` call site, and a 70 ms rate limit on
  the fallback (`awdlFbLastUs` / `awdlFbIdx`).

Build output goes to `build/itlmt7925.kext` and `build/itlmt7925Native.kext`.
