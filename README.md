# Jellyfin IPTV Async Preview — ZAPPING

**Unofficial, experimental Jellyfin Web Live TV thumbnail previews.****Anteprime IPTV asincrone per «In onda ora» di Jellyfin, con distribuzione push.**

This is an updated snapshot of the original project, now organized as **ZAPPING-JELLYFIN** and **ZAPPING-PROVIDERS**. It is **not** an official Jellyfin plugin or a substitute for Jellyfin's normal Live TV playback.

> **Security warning:** this software has **no independent Jellyfin-session authentication** for its HTTP/WebSocket endpoints, and its management API can restart processes. **Do not expose ports 8099, 8100 or 8110 to the public Internet.** Browser Origin checks are not authentication. See [Security](docs/SECURITY.md).

## What's new in this version

- Browser connects over **WebSocket** and sends its user's complete Jellyfin Favorites list, rather than polling for each JPEG.
- One separate **operating-system worker process per unique requested channel**. Two viewers of one channel share the acquisition.
- Workers request a frame from the **separate provider HTTP service**, publish each JPEG into a common input queue, then sleep independently before requesting again.
- One distributor dispatches frames to **per-client queues** according to subscriptions. Slow clients do not block source acquisition; pending images are replaced by newer ones for the same channel.
- The browser retains the latest frame in memory, so a Jellyfin DOM/card refresh does not automatically lose the thumbnail.
- The provider resolves HLS master/media playlists, follows redirects, prioritizes video variants, supports standard HLS `METHOD=AES-128` when its key is available, and returns distinct acquisition/decode errors.
- **SQLite remembers working source routes** across restarts (but the database itself is intentionally excluded from this repository).
- Errors result in bundled JPEG placeholders: `PROVIDER TIMEOUT`, `PROVIDER ERROR`, `DECODE ERROR`.

**Historical note:** The previous prototype used a small FFmpeg worker pool, HTTP-pulled thumbnails and disk cache. That architecture is superseded by this push version; do not apply the old README or service commands to these files.

## Components

| Component | Default port | Responsibility |
| --- | ---: | --- |
| `src/gestore_iptv.py` | `8100` | Supervisor, service status, administrative commands. |
| `src/server_auto.py` → `zapping_jellyfin.server` | `8099` | WebSocket sessions, Favorites, per-channel worker processes, input queue and distribution. **No HLS or FFmpeg here.** |
| `src/zapping_jellyfin/channel_worker.py` | no port | A separate process for each unique active channel; repeatedly requests `/frame` from provider, submits JPEG/errors via stdout IPC and sleeps. |
| `src/zapping_providers/server.py` | `8110` (loopback) | Playlist, HLS acquisition, AES-128 processing, FFmpeg one-frame decoding, persistent preferred routes. **No user sessions here.** |
| `src/tv_firenze_ora_auto.js` | browser | Reads Favorites through Jellyfin's Web API, subscribes to the WebSocket, receives JPEGs, updates cards and preserves last frames. |

```text
Jellyfin Web ── subscribe(Favorites) ──► ZAPPING-JELLYFIN :8099
                                              │
                       union of subscriptions │
                                              ▼
                      separate per-channel Python processes
                             │          │           │
                             └── HTTP GET /frame ───┘
                                        │
                              ZAPPING-PROVIDERS :8110
                                        │
                                 HLS → FFmpeg → JPEG
                                        │
                           worker stdout: (status, JPEG)
                                        │
                               shared ingress queue
                                        │
                                 single distributor
                                        │
                              queues per subscribed client
                                        │
                             WebSocket push → Jellyfin cards
```

One JPEG is decoded per channel, not per viewer; a client receives only the channels to which it subscribed. A new frame is delivered without the client issuing a new HTTP image request. The browser still re-reads Favorites from Jellyfin when necessary; this is separate from the video requests.

## Requirements

- Python **3.10+** (3.11+ recommended), `aiohttp>=3.9,<4`, Linux (process supervision requires the `fuser` utility from `psmisc`).
- FFmpeg, either on the host (`--ffmpeg ffmpeg`) or accessible inside a Jellyfin Docker container (`--ffmpeg 'docker exec -i jellyfin /usr/lib/jellyfin-ffmpeg/ffmpeg'`).
- A **private local playlist** containing the channel URLs and corresponding names; it is not included.
- A compatible Jellyfin Web **On Now** view and `ApiClient.getLiveTvChannels(...)` implementation. The DOM and this API are not stable integration points.
- For AES-128 HLS, either Python `cryptography` or an installed `openssl` binary. **DRM and `SAMPLE-AES` are not supported.**

## Quick start on a trusted LAN

The following commands are examples. Substitute **your own paths and server address**. Never publish your playlist or private configuration.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Optional: pip install -r requirements-aes128.txt
```

Start the supervisor (which launches the other two services). For a local FFmpeg binary:

```bash
python src/gestore_iptv.py \
  --host 192.168.1.20 \
  --origin http://192.168.1.20:8096 \
  --playlist /private/path/channels.m3u8 \
  --ffmpeg ffmpeg
```

For the **Docker FFmpeg** used in the original deployment, replace `--ffmpeg ffmpeg` with the full `docker exec ...` command above. The provider stays bound to `127.0.0.1:8110` by default. The manager's browser-facing endpoints need to be reachable from the browser, and firewall-restricted.

To integrate the browser side, follow [Deployment](docs/DEPLOYMENT.md); simply placing the JS file in the Jellyfin container is **not enough** if it is not already referenced by Jellyfin's web `index.html` or another loader.

Check services after opening **Live TV → On Now** in Jellyfin:

```bash
curl -fsS http://192.168.1.20:8100/status
curl -fsS http://192.168.1.20:8099/status
curl -fsS http://127.0.0.1:8110/status
```

With no subscribed browser, `clients=0`, `channels=0`, `workers=0` is normal. A user can have **62 Favorites and only 46 On Now cards**: subscription uses the complete Favorites list and display depends on which cards Jellyfin renders. No assumption of exactly 46 workers is built into the code.

## Timeouts and errors

| Step | Current default | Note |
| --- | ---: | --- |
| HTTP connect to HLS | 3 s | Within each `aiohttp` request. |
| HTTP socket read / total HLS request | 6 s / 8 s | Applies to *each* HLS HTTP request, not an entire playlist+segment chain. |
| FFmpeg decoding | 1 s | Per one-frame FFmpeg invocation; a short limit can reject slower valid streams. |
| Channel worker's provider request | 10 s | `urlopen` timeout, not a guaranteed whole-transaction deadline. |
| Channel worker sleep | 3 s | Independent per channel; no global scan timer. |

Failures are reported as `provider_error`, `provider_timeout`, or `decode_error` and produce diagnostic JPEGs. A single error counter or old status error does **not** alone prove that a visible card cannot display a previously received frame. For HTTP/API details, see [Protocol](docs/PROTOCOL.md).

## Source persistence

Working source routes are recorded under `src/zapping_providers/state/sources.sqlite3` (under the provider module's directory), **only after a valid JPEG has been decoded**. Playlist changes invalidate preferences by digest. The database can contain private stream URLs, so it must not be published. The resolver handles alternatives already present in the playlist, HLS variants, and known relinker variants; it **does not discover arbitrary new IPTV URLs from the Internet**.

## Documentation and testing

- [Italian guide](docs/README_IT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Deployment and rollback](docs/DEPLOYMENT.md)
- [Protocol and diagnostics](docs/PROTOCOL.md)
- [Security and privacy](docs/SECURITY.md)
- [Changes](CHANGELOG.md)

Offline/simulated tests (no production IPTV playlist needed):

```bash
PYTHONPATH=src python tests/test_routes.py
PYTHONPATH=src python tests/test_rai_redirect.py
PYTHONPATH=src python tests/test_hls_aes128.py
PYTHONPATH=src python tests/test_architettura.py
node --check src/tv_firenze_ora_auto.js
```

Tests use local temporary HTTP servers/ports; they are **not** proof of cross-platform compatibility, public-network security or correct playback for every real stream.

## Credit and license

Architecture and requirements by **Andrea**; prototype implementation and documentation developed collaboratively with ChatGPT. Community experiment, unaffiliated with the Jellyfin project. **MIT**, see [LICENSE](LICENSE).

For replacing an **existing GitHub repository** without uploading private runtime data, read [GitHub publishing guide (Italian)](docs/GITHUB_PUBLISH_IT.md).
