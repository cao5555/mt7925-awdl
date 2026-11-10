# Bluetooth beacon (`MtkBtFw` patch)

Apply on top of [`thanhnndev/mt7925-macos`](https://github.com/thanhnndev/mt7925-macos)
branch `fix/bt-download-retry` (the Bluetooth firmware loader lives there).

```sh
git apply /path/to/MtkBtFw-fe.patch
bash scripts/build.sh MtkBtFw
```

What it does, in order:

1. Claims USB `0E8D:0717` and walks its endpoints (`0x81` interrupt IN,
   `0x82` bulk IN, `0x02` bulk OUT).
2. Loads the firmware. **MediaTek HCI commands go to the control endpoint
   (`class request 0x20`) without the packet-type byte**; events arrive on the
   interrupt endpoint with a 16-byte max packet size.
3. Probes the controller (`ReadLocalVersion` → HCI 5.4, MediaTek `0x0046`).
4. Programs LE **extended** advertising, because every legacy command returns
   `0x0C Command Disallowed`:
   * `LE Set Extended Advertising Parameters` (`0x2036`) — legacy PDU,
     connectable + scannable (`0x13`), 100 ms interval, all channels;
   * `LE Set Extended Advertising Data` (`0x2037`) — the 30-byte AD below;
   * `LE Set Extended Advertising Enable` (`0x2039`).
5. Sleeps one hour, then releases the interface to macOS.

The advertisement:

```
02 01 06                                              Flags (LE general discoverable)
17 ff 4c 00 05 12 40 <tag3> 00000000 03 <8 bytes> 00   AirDrop TLV, Apple 0x004C
02 09 4d                                              Complete local name "M"
```

`tag3` and the eight hash bytes are zeros: that is the "no contact match"
form, which is what an **Everyone** receiver accepts. Use real 2-byte contact
hashes if you need Contacts Only.
