#!/bin/sh
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"

# Clean up stale X11 locks if container or subshell restarted
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

# Start Xvfb; xkbcomp keysym warnings are cosmetic version-skew noise, not errors
{ Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp -ac 2>&1 | grep -vE "Could not resolve keysym|^Warnings|^Errors from xkbcomp"; } &
export DISPLAY=":${DISPLAY_NUM}"

# Wait for X socket readiness
for _ in $(seq 1 50); do
    DISPLAY=":${DISPLAY_NUM}" xdotool getdisplaygeometry >/dev/null 2>&1 && break
    [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.1
done

# Window manager so headed --start-maximized and window geometry are honored
{ DISPLAY=":${DISPLAY_NUM}" openbox 2>&1 | grep -v "menu file"; } &

# Fewer glibc malloc arenas: keeps the Python process's reserved address space small (harmless hardening)
export MALLOC_ARENA_MAX=2

# --loop asyncio is what fixes `OSError: [Errno 12] Cannot allocate memory` on browser launch. Playwright starts its
# node driver with asyncio subprocess; uvloop does that with fork(), and the kernel (overcommit heuristic mode)
# refuses to fork a process holding one merged writable mapping larger than RAM+swap (the Go runtime inside
# httpcloak grows such a mapping, ~6 GB here with only ~10 MB resident). The default loop uses vfork, which never
# copies the parent's address space, so the launch cannot fail on it.
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --loop asyncio --timeout-graceful-shutdown 20 "$@"
