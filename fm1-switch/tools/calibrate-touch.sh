#!/bin/sh
# Record which raw X/Y the panel reports when you press each of its four corners.
#
# Why this exists: the dtoverlay ranges shipped by Waveshare are a starting point, not a
# measurement. If they are wrong the pointer lands short of an edge -- you cannot tap the
# top row or the right-hand button -- and that reads as "touch is broken" rather than
# "calibration is off". Run this, press each corner, then the values go into config.txt.
#
# Usage: ./calibrate-touch.sh [seconds-per-corner]

N=${1:-6}
DEV=/dev/input/event0

if [ ! -e "$DEV" ]; then
    echo "No $DEV -- the touch controller is not present. Ask for check-touch.sh." >&2
    exit 1
fi

echo "For each corner you get $N seconds. Press it firmly with a STYLUS or fingernail"
echo "and HOLD, then move to the next when prompted. A light fingertip may not register"
echo "on a resistive panel."
echo

for corner in "TOP-LEFT" "TOP-RIGHT" "BOTTOM-LEFT" "BOTTOM-RIGHT"; do
    printf '%s: press and hold that corner now… ' "$corner"
    # evtest prints "Event: time … EV_ABS … ABS_X … value NNNN" lines. Take the extremes
    # over the window rather than the last sample: a wobbling stylus ends anywhere.
    out=$(timeout "$N" evtest "$DEV" 2>/dev/null | grep -E "ABS_(X|Y)" | awk '
        /ABS_X/ { for(i=1;i<=NF;i++) if($i=="value") x=$(i+1); if (xmin==""||x<xmin) xmin=x; if (xmax==""||x>xmax) xmax=x }
        /ABS_Y/ { for(i=1;i<=NF;i++) if($i=="value") y=$(i+1); if (ymin==""||y<ymin) ymin=y; if (ymax==""||y>ymax) ymax=y }
        END { if (xmin!="") printf "x=%s..%s y=%s..%s\n", xmin, xmax, ymin, ymax }')
    if [ -z "$out" ]; then
        echo "no touch seen"
    else
        echo "$out"
    fi
done
echo
echo "Report those four lines back. The extremes across all four corners give xmin/xmax/"
echo "ymin/ymax for dtoverlay=ads7846."
