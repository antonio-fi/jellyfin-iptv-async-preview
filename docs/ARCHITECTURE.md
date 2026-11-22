# Architecture — ZAPPING PUSH

## Process and trust boundaries

```text
Browser Jellyfin Web
  ├─ Jellyfin API: getLiveTvChannels({IsFavorite:true,Limit:1000})
  └─ WS /ws (name list, generated client ID) 
          ↓
ZAPPING-JELLYFIN (:8099)
  ├─ session registry: socket -> requested channel names
  ├─ set union: one active channel process per unique requested name
  ├─ channel process: ZAPPING-JELLYFIN/channel_worker.py (one OS process)
  │     └─ HTTP GET /frame?name=... to Providers (:8110)
  ├─ ingress asyncio.Queue (maxsize 128, older item dropped if full)
  ├─ latest JPEG in memory per channel
  ├─ distributor: ingests completed JPEG and fans out by subscription
  └─ per-client pending dict of channel -> latest JPEG
          ↓
Browser receives binary WS frames, patches matching On Now cards

ZAPPING-PROVIDERS (:8110, loopback)
  ├─ local M3U (not stored in Git)
  ├─ source candidate selection + redirect-relative HLS parsing
  ├─ shared aiohttp session, request limits and HLS AES-128 support
  ├─ temporary FFmpeg subprocess for each new segment/frame
  └─ working-source SQLite store (not stored in Git)

Supervisor (:8100): starts/monitors these service processes; admin restart.
```

## Subscription lifecycle

1. User opens **On Now**. JS obtains Favorites from the current Jellyfin Web API context. This API response may contain more channels than are visible in the current view.
2. JS opens one WebSocket and sends `subscribe` with a generated client ID and the Favorite names.
3. The server merges the subscription sets. It starts/retains one channel worker per distinct name.
4. Each worker requests a single JPEG, sends `status byte + big-endian JPEG size + JPEG bytes` over its own stdout pipe, sleeps per channel, and repeats.
5. The ingress queue is drained by a separate distributor; each interested client gets its latest JPEG queued by channel.
6. Sender tasks emit one binary message for each queued channel update. Slow clients' pending entries can be overwritten; an in-flight `send_bytes` may still wait on transport backpressure.
7. When clients leave, their subscriptions disappear and unnecessary workers are cancelled. A new client can receive in-memory latest frames immediately.

The architecture isolates **logical channel scheduling**, not all computer resources. Many concurrent FFmpeg processes may still saturate CPU, RAM, Docker command startup, or the network.

## Separation

- `zapping_jellyfin` never parses IPTV playlists, downloads video segments or invokes FFmpeg.
- `zapping_providers` never reads user Favorites or maintains WebSocket client subscriptions.
- The shared `channel_worker.py` process is an IPC/HTTP bridge owned by ZAPPING-JELLYFIN. It does **not** decode HLS itself.
- `gestore_iptv.py` supervises both and exposes status/restart. The old `server_auto.py` path is retained as a compatibility entry point for the Jellyfin side.

## Provider source resolver

1. Read sources from a private M3U file (channel names from EXTINF). Respect explicit alternate entries under the same channel name.
2. Prefer the previously confirmed route if its source playlist digest is unchanged.
3. Follow HTTP redirects and resolve HLS variant and segment paths relative to the **effective** response URL.
4. Prioritize explicitly video-bearing HLS variants. For standard `EXT-X-KEY:METHOD=AES-128`, retrieve the available key, decrypt a segment and pass it to FFmpeg. DRM/SAMPLE-AES is not handled.
5. Create a temporary FFmpeg process; produce one JPEG with a 1-second default timeout.
6. On a valid JPEG, update SQLite only when the preferred route changes. On failures, apply in-memory cooldown and try known alternatives.

**Not implemented:** arbitrary internet searches for new IPTV streams; authenticated Jellyfin user-session enforcement; general DASH/DRM; guaranteed support of all HLS extensions or provider-specific web sessions.

## Legacy differences

The prior prototype documented `/state` and repeated `/frame` polling from the browser, a shared fixed decoder pool, disk JPEG caching, hover priority and a response-rate LED. These descriptions are **obsolete** in the push architecture. Read [Protocol](PROTOCOL.md) for current endpoints.
