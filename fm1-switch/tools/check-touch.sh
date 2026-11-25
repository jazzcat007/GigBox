#!/bin/sh
# Report whether the panel's touch controller came up, and in what shape.
#
# Run on the Pi after enabling dtoverlay=ads7846. Exists because the failure mode is
# SILENT: the overlay loads, the console looks fine, and no touch device is created. That
# looks identical to "touch is broken", so this checks each link in the chain separately.

echo "=== SPI enabled? ==="
if [ -e /sys/bus/spi/devices ]; then
    for d in /sys/bus/spi/devices/*; do
        [ -e "$d" ] || continue
        echo "  $(basename "$d")"
    done
    [ -z "$(ls /sys/bus/spi/devices)" ] && echo "  (bus present, no devices)"
else
    echo "  NO SPI BUS — dtparam=spi=on missing or not applied"
fi

echo
echo "=== touch input device? ==="
if evtest --list-devices 2>/dev/null | grep -qi "ads7846\|Touchscreen\|XPT2046"; then
    evtest --list-devices 2>/dev/null | grep -i "ads7846\|Touchscreen\|XPT2046" | sed 's/^/  /'
    echo "  -> TOUCH PRESENT"
else
    echo "  none found. Check:"
    echo "   - dtoverlay=ads7846 line in /boot/firmware/config.txt"
    echo "   - penirq GPIO (Waveshare 5\" HDMI: 25)"
    echo "   - the 26-pin ribbon is actually seated on the Pi's 40-pin header"
    echo "  -> TOUCH ABSENT"
fi

echo
echo "=== all input devices ==="
cat /proc/bus/input/devices | grep -E "^N: Name|^H: Handlers" | sed 's/^/  /'

echo
echo "=== kernel messages about ads7846/spi ==="
dmesg 2>/dev/null | grep -iE "ads7846|spi0|xpt2046" | tail -6 || echo "  (dmesg needs root)"

echo
echo "=== console size (should be ~85x32 at Terminus 24x12) ==="
stty -F /dev/tty1 size 2>/dev/null || echo "  (cannot read tty1)"
