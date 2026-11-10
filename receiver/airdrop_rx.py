#!/usr/bin/env python3
"""Minimal AirDrop receiver: TLS + the /Discover, /Ask and /Upload endpoints over awdl0."""
import socket, ssl, struct, plistlib, os, time, sys, threading

PORT = 8770
SO_RECV_ANYIF = 0x1104
INBOX = os.path.expanduser("~/Downloads/AirDropInbox")
DOWNLOADS = os.path.expanduser("~/Downloads")
os.makedirs(INBOX, exist_ok=True)

INFO = {
    "ReceiverComputerName": "cao-5555s-Mac-Pro",
    "ReceiverModelName": "MacPro7,1",
    "ReceiverMediaCapabilities": b'{"Version":1}',
    # macOS senders ignore these; iOS has been observed to stop after /Discover without them.
    "StatusCode": 100,
    "IsAirDropable": True,
}

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def read_head(rfile):
    line = rfile.readline()
    if not line:
        return None, None, None
    log("request line:", line[:80])
    try:
        method, path, _ = line.decode("latin1").split(" ", 2)
    except ValueError:
        return None, None, None
    headers = {}
    while True:
        h = rfile.readline()
        if not h or h in (b"\r\n", b"\n"):
            break
        k, _, v = h.decode("latin1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return method, path, headers


def read_body(rfile, headers):
    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            sz = rfile.readline().strip()
            if not sz:
                break
            n = int(sz.split(b";")[0], 16)
            if n == 0:
                while True:                       # consume the trailer: an empty line ends the body
                    t = rfile.readline()
                    if not t or t in (b"\r\n", b"\n"):
                        break
                break
            chunk = rfile.read(n)
            body += chunk
            if len(body) % 16384 < n or n < 1024:
                log(f"  chunk {len(chunk)} total {len(body)}")
    elif "content-length" in headers:
        need = int(headers["content-length"])
        last = 0
        while len(body) < need:
            chunk = rfile.read(min(65536, need - len(body)))
            if not chunk:
                break
            body += chunk
            if len(body) - last >= 16384:
                last = len(body)
                log(f"  body progress {len(body)}/{need}")
    return body


def respond(wfile, status, body=b"", ctype=None, extra=""):
    # opendrop's receiver sends no Content-Type at all; AirDrop's client accepts the bare plist.
    hdr = f"HTTP/1.1 {status}\r\n"
    if ctype:
        hdr += f"Content-Type: {ctype}\r\n"
    hdr += f"Content-Length: {len(body)}\r\n"
    hdr += extra + "\r\n"
    wfile.write(hdr.encode() + body)
    wfile.flush()


def unpack_dvzip(body, dest):
    """Turn the /Upload body (gzip'd CPIO, odc or newc) into files under dest.

    ASK-5: names come from the sender, so only the base name is kept and anything
    that would escape dest is dropped.
    """
    import gzip
    data = body
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception as e:
            log("gzip failed:", e)
            return []
    saved = []
    i = 0
    while i + 76 <= len(data):
        magic = data[i:i + 6]
        if magic == b"070707":                       # odc: fixed 76-byte octal header
            def fld(a, b):
                return int(data[i + a:i + b], 8)
            mode = fld(18, 24); namesize = fld(59, 65); filesize = fld(65, 76)
            i += 76
            name = data[i:i + namesize - 1].decode("utf-8", "replace"); i += namesize
            payload = data[i:i + filesize]; i += filesize
        elif magic in (b"070701", b"070702"):        # newc: 110-byte hex header
            def fld(a, b):
                return int(data[i + a:i + b], 16)
            namesize = fld(94, 102); filesize = fld(54, 62)
            i += 110
            name = data[i:i + namesize - 1].decode("utf-8", "replace"); i += namesize
            i = (i + 3) & ~3
            payload = data[i:i + filesize]; i += filesize
            i = (i + 3) & ~3
        else:
            log("cpio: bad magic", magic)
            break
        if name == "TRAILER!!!":
            break
        base = os.path.basename(name)
        if not base or base in (".", "..") or name.endswith("/") or base.startswith("._"):
            continue                                  # root entry, directories and AppleDouble metadata
        out = os.path.join(dest, base)
        with open(out, "wb") as f:
            f.write(payload)
        saved.append(out)
        log("extracted", out, len(payload), "bytes")
    return saved

def handle(conn, addr):
    conn.settimeout(120)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(os.path.dirname(__file__), "cert.pem"),
                        os.path.join(os.path.dirname(__file__), "key.pem"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        tls = ctx.wrap_socket(conn, server_side=True)
        log("tls", tls.version(), tls.cipher()[0])
    except ssl.SSLError as e:
        log("tls handshake SSLError:", e, "verify:", getattr(e, "verify_message", ""))
        return
    except Exception as e:
        log("tls handshake failed:", e)
        return
    log("TLS ok from", addr[0])
    rf = tls.makefile("rb")
    wf = tls.makefile("wb")
    log("handler start")
    asked = False
    for _ in range(8):
        try:
            method, path, headers = read_head(rf)
        except socket.timeout:
            log("read timeout (no request)")
            break
        if method is None:
            break
        # UP-3: a sender that expects 100-continue waits for it before sending the body.
        if headers.get("expect", "").lower() == "100-continue":
            wf.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            wf.flush()
            log("sent 100 Continue")
        # /Upload: answer at once. The sender has been observed to hold the body until it gets a response on
        # the pipelined request, and a 200 before the body arrives still lets it finish the upload.
        if path.startswith("/Upload") and not headers.get("expect"):
            respond(wf, "200 OK")
            log("answered /Upload before the body")
        try:
            body = read_body(rf, headers)
        except socket.timeout:
            log("read timeout (body)")
            break
        if headers:
            log(f"  headers: {headers}")
        log(f"{method} {path} body={len(body)}")
        if path.startswith("/Discover"):
            open(os.path.join(INBOX, "discover_request.bin"), "wb").write(body)
            try:
                req = plistlib.loads(body)
                log("Discover keys:", sorted(req.keys()))
            except Exception as e:
                log("Discover plist parse failed:", e)
            resp = plistlib.dumps(INFO, fmt=plistlib.FMT_BINARY)
            log("Discover response:", len(resp), "bytes")
            respond(wf, "200 OK", resp)
        elif path.startswith("/Ask"):
            asked = True
            req = {}
            try:
                req = plistlib.loads(body)
            except Exception:
                pass
            name = req.get("SenderComputerName", "?")
            log("Ask from", name, "files:", [f.get("FileName") for f in req.get("Files", [])])
            respond(wf, "200 OK", plistlib.dumps({"ReceiverComputerName": INFO["ReceiverComputerName"],
                                                  "ReceiverModelName": INFO["ReceiverModelName"]},
                                                 fmt=plistlib.FMT_BINARY))
        elif path.startswith("/Upload"):
            if not asked:
                respond(wf, "400 Bad Request"); break
            out = os.path.join(INBOX, f"upload-{int(time.time())}.dvzip")
            with open(out, "wb") as f:
                f.write(body)
            log("saved", out, len(body), "bytes")
            saved = unpack_dvzip(body, DOWNLOADS)
            log("upload complete:", len(saved), "file(s) in", DOWNLOADS)
            respond(wf, "200 OK")
        else:
            respond(wf, "404 Not Found")
        if headers.get("connection", "").lower() == "close":
            log("peer asked for close")
            break
    try:
        tls.close()
    except Exception:
        pass

def main():
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, SO_RECV_ANYIF, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("::", PORT))
    s.listen(8)
    log(f"airdrop_rx listening on [{PORT}]")
    while True:
        c, a = s.accept()
        log("accept from", a[0])
        threading.Thread(target=_run, args=(c, a), daemon=True).start()

def _run(c, a):
    try:
        handle(c, a)
    except Exception as e:
        log("handler error:", e)
    finally:
        try: c.close()
        except Exception: pass

main()
