# Receiver

Two programs run side by side:

```sh
clang -O2 -o awdlsvc awdlsvc.c
./awdlsvc advertise-only &        # registers _airdrop._tcp on awdl0
python3 airdrop_rx.py &           # TLS server on :8770
```

`awdlsvc` uses `DNSServiceRegister` with `kDNSServiceFlagsIncludeAWDL`, so the
service shows up on `awdl0` (interface index 10) even when macOS's Bluetooth is
off. The instance name is a stable 12-hex hash: a random name per start left
stale registrations behind and senders saw several copies of the machine.

`airdrop_rx.py` speaks AirDrop's HTTP over TLS 1.2 with a self-signed
certificate:

* `POST /Discover` → binary plist with `ReceiverComputerName`,
  `ReceiverModelName`, `ReceiverMediaCapabilities`, `StatusCode` and
  `IsAirDropable`;
* `POST /Ask` → `200` with `ReceiverComputerName` / `ReceiverModelName`;
* `POST /Upload` → answers `100 Continue` when asked, reads the body
  (`Content-Length` or chunked), stores the archive in
  `~/Downloads/AirDropInbox/` and unpacks it into `~/Downloads`.

The archive is gzip'ed CPIO (`odc` or `newc`); `.` and `._*` members are
skipped and only base names are kept.

Generate the certificate once:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 365 -subj "/CN=AirDrop"
```
