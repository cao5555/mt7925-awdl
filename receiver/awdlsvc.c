// awdlsvc: register an AirDrop-style _airdrop._tcp service with the AWDL flag and listen on its port.
// Purpose: advertise a receiver while our kext holds the Bluetooth controller (macOS's sharingd can't).
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <dns_sd.h>

#ifndef SO_RECV_ANYIF
#define SO_RECV_ANYIF 0x1104
#endif

static void cb(DNSServiceRef ref, DNSServiceFlags flags, DNSServiceErrorType err,
               const char *name, const char *type, const char *domain, void *ctx) {
    printf("awdlsvc: register cb err=%d name=%s type=%s domain=%s flags=0x%x\n",
           err, name ? name : "-", type ? type : "-", domain ? domain : "-", flags);
}

int main(int argc, char **argv) {
    uint16_t port = 8770;
    // A stable instance name: a fresh random one on every start left stale registrations behind, so a peer
    // saw two receivers (and later picked the dead one).
    char name[32];
    unsigned long long h = 1469598103934665603ULL;            // FNV-1a over the host name
    for (const char *c = "cao-5555s-Mac-Pro"; *c; c++) { h ^= (unsigned char)*c; h *= 1099511628211ULL; }
    snprintf(name, sizeof(name), "%012llx", h & 0xffffffffffffULL);

    char txtbuf[32];
    const char *kv = "flags=141";              // 0x08d: + SupportsPipelining (UP-8): the sender may pipeline /Upload
    txtbuf[0] = (char)strlen(kv);
    memcpy(txtbuf + 1, kv, strlen(kv));

    DNSServiceRef sdRef = NULL;
    DNSServiceErrorType e = DNSServiceRegister(&sdRef,
        kDNSServiceFlagsIncludeAWDL | kDNSServiceFlagsNoAutoRename, 0,
        name, "_airdrop._tcp.", "local.", NULL, htons(port),
        (uint16_t)(1 + strlen(kv)), txtbuf, cb, NULL);
    printf("awdlsvc: DNSServiceRegister err=%d name=%s\n", e, name);
    if (e != kDNSServiceErr_NoError) return 1;

    if (argc > 1 && strcmp(argv[1], "advertise-only") == 0) {
        printf("awdlsvc: advertise-only mode\n");
        for (;;) {
            int dn = DNSServiceRefSockFD(sdRef);
            if (dn < 0) { sleep(1); continue; }
            fd_set rf; FD_ZERO(&rf); FD_SET(dn, &rf);
            if (select(dn + 1, &rf, NULL, NULL, NULL) > 0 && FD_ISSET(dn, &rf)) DNSServiceProcessResult(sdRef);
        }
    }
    int fd = socket(AF_INET6, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_RECV_ANYIF, &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in6 a; memset(&a, 0, sizeof(a));
    a.sin6_family = AF_INET6; a.sin6_port = htons(port); a.sin6_addr = in6addr_any;
    printf("awdlsvc: bind rc=%d errno=%d port=%u\n", bind(fd, (struct sockaddr *)&a, sizeof(a)), errno, port);
    listen(fd, 8);
    printf("awdlsvc: listening\n");

    for (;;) {
        int dn = DNSServiceRefSockFD(sdRef);
        fd_set rf; FD_ZERO(&rf);
        FD_SET(fd, &rf);
        if (dn >= 0) FD_SET(dn, &rf);
        int maxfd = (dn > fd ? dn : fd) + 1;
        if (select(maxfd, &rf, NULL, NULL, NULL) < 0) break;
        if (dn >= 0 && FD_ISSET(dn, &rf)) DNSServiceProcessResult(sdRef);
        if (FD_ISSET(fd, &rf)) {
            struct sockaddr_in6 c; socklen_t l = sizeof(c);
            int s = accept(fd, (struct sockaddr *)&c, &l);
            if (s < 0) continue;
            unsigned char buf[64]; ssize_t n = read(s, buf, sizeof(buf));
            char host[INET6_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET6, &c.sin6_addr, host, sizeof(host));
            printf("awdlsvc: conn from %s read=%zd first=", host, n);
            for (int i = 0; i < n && i < 8; i++) printf("%02x ", buf[i]);
            printf("\n"); fflush(stdout);
            close(s);
        }
    }
    return 0;
}
