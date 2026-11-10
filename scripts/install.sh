#!/bin/sh
# Install the two patched kexts into the ESP. Run as root; edit ESPDISK first.
#   diskutil list | grep "EFI ESP"       # find the device, e.g. disk2s1
ESPDISK=${ESPDISK:-disk2s1}
SRC=${SRC:-/Users/cao-5555/Documents/deepseek-harness/default-workspace}
VOL=/Volumes/ESP_EFI

diskutil mount "$ESPDISK" || exit 1
for k in itlmt7925 itlmt7925Native; do
    [ -d "$SRC/mt7925-upstream/build/$k.kext" ] || { echo "missing $k"; continue; }
    rm -rf "$VOL/EFI/OC/Kexts/$k.kext"
    cp -R "$SRC/mt7925-upstream/build/$k.kext" "$VOL/EFI/OC/Kexts/"
    ls -l "$VOL/EFI/OC/Kexts/$k.kext/Contents/MacOS/"
done
if [ -d "$SRC/mt7925-btfix/build/MtkBtFw.kext" ]; then
    rm -rf "$VOL/EFI/OC/Kexts/MtkBtFw.kext"
    cp -R "$SRC/mt7925-btfix/build/MtkBtFw.kext" "$VOL/EFI/OC/Kexts/"
    ls -l "$VOL/EFI/OC/Kexts/MtkBtFw.kext/Contents/MacOS/"
fi
diskutil unmount "$ESPDISK"
