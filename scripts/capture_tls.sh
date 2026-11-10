#!/bin/bash
# Capture the AirDrop TLS exchange on awdl0 port 8770 for 90 seconds.
# Run as root. Trigger an AirDrop send from the iPhone while it captures.
W=/Users/cao-5555/Documents/deepseek-harness/default-workspace
OUT=$W/awdl-tls.pcap
rm -f "$OUT"
echo ">> capturing TCP 8770 on awdl0 for 90 s; start the AirDrop send now"
tcpdump -i awdl0 -n -s 0 -w "$OUT" 'tcp port 8770' &
PID=$!
sleep 90
kill -INT $PID 2>/dev/null
sleep 1
chmod 644 "$OUT"
echo "wrote $OUT: $(ls -l "$OUT" | awk '{print $5}') bytes"
tcpdump -r "$OUT" -n 2>/dev/null | head -20
