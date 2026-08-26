#!/bin/sh
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"

# Clean up stale X11 locks if container or subshell restarted
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

# Start Xvfb virtual display
Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp -ac &
export DISPLAY=":${DISPLAY_NUM}"

# Wait for X socket readiness
for _ in $(seq 1 50); do
    [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.1
done

exec uvicorn main:app --host 0.0.0.0 --port 8000 "$@"
