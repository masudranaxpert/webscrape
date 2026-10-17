# Browsers, binaries & license

CloakBrowser ships two Chromium builds. Which one a browser process runs depends on the license and on whether that
process is the **default browser**.

| Build | Where it comes from | Who runs it |
| --- | --- | --- |
| **Keyless** (pinned by the installed `cloakbrowser` package; Chromium 146 today) | Baked into the Docker image | Every browser that does not hold the license seat |
| **Pro** (the latest build the license server offers; Chromium 154 today) | Downloaded once with a valid key, kept in the `cloak-cache` volume | Exactly one browser: the seat holder |

The free plan has **one seat**: one Pro browser at a time across every machine using that key.

## Which browser gets which build

The **default browser** is the one requests without a `proxy` field escalate to: the WARP browser when
`WARP_PROXY` is set, the direct browser otherwise.

| `CLOAKBROWSER_LICENSE_KEY` | `WARP_PROXY` | Default browser | Direct (`proxy:"direct"`) | Any request proxy |
| --- | --- | --- | --- | --- |
| empty | unset | direct → keyless | (is the default) | keyless |
| empty | set | WARP → keyless | keyless | keyless |
| valid, seat free | unset | direct → **Pro** | (is the default) | keyless |
| valid, seat free | set | WARP → **Pro** | keyless | keyless |
| valid, seat busy elsewhere | any | keyless, retried later | keyless | keyless |
| invalid / expired | any | keyless | keyless | keyless |

So: **only the default browser can ever be Pro; every proxy browser is always keyless; no key means keyless
everywhere.**

Why only one candidate: when two browsers both tried for the single seat, the second spent ~9 s per launch in
"session limit" retries and ended up keyless anyway.

## Lifecycle

| Browser | Started | Closed |
| --- | --- | --- |
| Default (WARP when set, else direct) | At server start, in the background; relaunched in the background after a crash or when its lifetime ends | Never |
| Any other (direct while WARP is set, each proxy) | On the first request that needs it | After `BROWSER_IDLE_SHUTDOWN_SEC` (10 min) without a tab, or when `MAX_BROWSERS` needs its slot |

## `MAX_BROWSERS`: proxy browsers only

`MAX_BROWSERS` is the number of **proxy browsers** (requests that send a proxy URL) that may run at once. It has no
effect on the default browser or the direct browser: those are never counted, never refused and the default is
never closed.

| Browser | Counts toward `MAX_BROWSERS`? | Can be closed to make room? |
| --- | --- | --- |
| Default (WARP when set, else direct) | No | Never |
| Direct (when WARP is the default) | No | No (closes only after 10 min idle) |
| Each proxy URL | **Yes** | Yes, the least recently used idle one, when a new proxy needs the slot |

With `MAX_BROWSERS=1`, one proxy browser runs next to the default (and direct); a second, different proxy replaces
the first if it is idle, or waits. `MAX_BROWSERS=0` disables proxy browsers (409) while the default and direct keep
working. Each proxy browser costs ~400 MB.

## What happens when the seat is not available

| Situation | Behaviour |
| --- | --- |
| Seat held by a just-closed process | Two retries 2.5 s apart (the license server needs a moment to free it) |
| Seat busy for longer (another machine, a local run with the same key) | Launches keyless; the licensed build is tried again after 5 min |
| Server says "invalid or expired" | Keyless; the licensed build is not tried again for 10 min (the free plan reports a seat busy elsewhere this way too) |
| License server unreachable | Keyless |

!!! warning "Local runs take the production seat"
    `load_dotenv()` reads `CLOAKBROWSER_LICENSE_KEY` from a local `.env`. Any local browser test with the production
    key takes the one seat away from the server. Run tests with `CLOAKBROWSER_LICENSE_KEY=` (empty).

## User-Agent follows the build

The keyless build reports `Chrome/146`, the Pro build `Chrome/154`. After a solve, the HTTP engine replays cookies
with the UA **of the browser that earned them** and the matching `httpcloak` preset (`chrome-146-*` or
`chrome-154-*`). That is why `meta.user_agent` / `meta.preset` differ between a WARP-solved host and a host solved
through a proxy.

## Updates and disk

Launches never download (`CLOAKBROWSER_AUTO_UPDATE=false`): a request must not wait for a 700 MB download.
Instead `app/core/binaries.py` runs at start-up and every 24 h, holding the pool's launch lock so no Chromium starts
mid-download:

1. **Keyless:** makes sure the build pinned by the installed package exists. Upgrading `cloakbrowser` in
   `requirements.txt` changes that pin; the new build is downloaded on the next start.
2. **Pro:** with a valid key, moves to the server's latest Pro build (`check_for_pro_update`). Free keys always get
   the latest build from the server.
3. **Prune:** deletes every other `chromium-*` build, except builds a running browser still uses. A browser started
   on an older build keeps it until it is recycled; the next prune removes it.

The cache directory `~/.cloakbrowser` is the `cloak-cache` Docker volume. A new volume is seeded from the image (the
baked keyless build and GeoIP DB); the Pro build is downloaded the first time and then survives every deploy.
Expect ~1.5 GB: one keyless build, one Pro build, the GeoIP DB.

| Edge case | Behaviour |
| --- | --- |
| First start on a new volume with a key | The Pro download (~760 MB) happens in the background. Launches wait on the launch lock; with a slow link the first browser launch can hit its 90 s ceiling (502) until the download finishes. HTTP requests are unaffected |
| New `cloakbrowser` release in `requirements.txt` | Next start downloads the new keyless build, then prunes the old one |
| New Pro build on the server | Picked up within 24 h (or at the next restart); running browsers switch when recycled |
| Key removed | Pro build stays cached but unused; it is pruned only when a different Pro build replaces it |
| Volume deleted | Re-seeded from the image; Pro downloaded again |
