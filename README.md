# Jellyfin IPTV Async Preview (experimental)

An **unofficial proof of concept** for Live TV thumbnail previews on Jellyfin Web's **On Now** programme cards.

A lightweight Python service uses an asynchronous HTTP client to fetch ordinary **unencrypted HLS** playlists and segments concurrently. A small, separately bounded FFmpeg decoder pool produces JPEG stills. JavaScript displays the stills inside Jellyfin's existing programme cards without making programme navigation wait for preview acquisition.

## Features

- First available thumbnail appears in each programme card as it becomes available.
- A channel-name badge stays visible at the top-left of the card, even before an image arrives.
- Hovering a card for 500 ms prioritizes continuous preview refresh for that channel; after leaving, the last displayed image remains.
- A header LED shows the **rate of completed IPTV HTTP responses** measured by the Python service; it does *not* represent decoded frames or playback activity.
- A single `aiohttp` client manages multiple logical source sessions; decoder concurrency is limited independently.
- JPEGs are cached on disk by channel name.
- Live TV playback and preview fetching are **independent**. This project does not prebuffer video for the Jellyfin player.

## Status and compatibility

**Prototype**, manually tested on one Jellyfin **10.11.11** Docker/Linux installation, with custom favourites filtering present in that installation. This is **not** a Jellyfin plugin, an official change, or a tested cross-version deployment. Different Jellyfin Web DOM structures may require adjustment. The code relies on Jellyfin Web's `ApiClient.getLiveTvChannels(...)` and programme cards with `data-channelid` attributes; these are not guaranteed stable APIs.

Initial observations from one user: a sequential first prototype could take more than ten minutes for a full scan; the multisession version showed thumbnails in approximately **6–7 seconds** on one occasion. These observations are **not benchmarks** or promises of similar performance elsewhere.

## Requirements

- Python 3.10+ and [`aiohttp`](https://pypi.org/project/aiohttp/) 3.x.
- FFmpeg installed locally, or a compatible FFmpeg command accessible from the host.
- A local `.m3u`/`.m3u8` channel playlist where each supported channel name maps to exactly one HTTP(S) HLS URL.
- Jellyfin Web with compatible Live TV **On Now** programme cards.
- Browser access to the preview server from the exact Jellyfin web origin. HTTPS Jellyfin deployments **need an HTTPS reverse proxy** for the preview service to avoid mixed-content blocking.

## Try it on a trusted LAN only

**Do not expose port 8099 publicly.** There is no Jellyfin authentication on this standalone prototype service. The origin check is **not an authentication mechanism**; it limits browser CORS, not non-browser requests. Restrict network access to trusted clients, preferably behind an authenticated reverse proxy before wider deployment. Use only streams you are authorized to access.

1. Make an isolated environment:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Provide your own local HLS playlist (not included here). Start the Python service with **your** paths and host/origin values:

   ```bash
   python3 src/server_auto.py \
     --playlist /path/to/your/channels.m3u8 \
     --cache ./cache \
     --host 192.168.1.20 \
     --origin http://192.168.1.20:8096 \
     --port 8099 \
     --ffmpeg ffmpeg
   ```

   By default `--host` is `127.0.0.1` and the service is **not remotely reachable**. For FFmpeg inside a Docker Jellyfin container, you can instead pass `--ffmpeg 'docker exec -i jellyfin /usr/lib/jellyfin-ffmpeg/ffmpeg'` if that command works on your installation.

3. Make `src/tv_firenze_ora_auto.js` available as `/web/tv_firenze_ora_auto.js` from Jellyfin's **actual** web document root. Add the following line **just before `</head>`** in a backed-up copy of Jellyfin's web `index.html`:

   ```html
   <script defer src="/web/tv_firenze_ora_auto.js"></script>
   ```

   See [manual Docker deployment](docs/DEPLOYMENT.md) before changing a container. This project does **not** ship an automated installer that overwrites Jellyfin assets.

4. Hard-refresh Jellyfin (Ctrl+F5), open **Live TV → On Now** and inspect the browser console, the LED, and `http://YOUR_HOST:8099/status` from a trusted client.

5. To undo the browser integration, restore your backed-up `index.html`, remove the extra JS from Jellyfin's web root and stop the Python process. Your Jellyfin favourites, playlist and player are not intentionally modified by this proof of concept.

### Optional JS service address

The browser defaults to `location.protocol + '//' + location.hostname + ':8099'`. If your proxy exposes the service elsewhere, set `window.jellyfinPreviewBase` **before** the script is loaded, e.g. in `index.html`:

```html
<script>window.jellyfinPreviewBase = '/iptv-preview';</script>
<script defer src="/web/tv_firenze_ora_auto.js"></script>
```

A proxy must forward `/iptv-preview/...` to the preview server, remove the prefix if necessary, and apply authentication/access controls as appropriate.

## Architecture

```text
Jellyfin Web On Now cards ─── async heartbeat + /state + /frame ──┐
                                                                │
HLS servers → shared aiohttp network client → per-channel tasks │
                                  │                             │
                            completed segment                   │
                                  ↓                             │
                         bounded FFmpeg pool                    │
                                  ↓                             │
                       per-channel JPEG + disk cache ←──────────┘

Hover → /focus → one continuous high-priority preview stream
```

The current receiver supports simple HLS master/media playlists and a single segment extraction path. It does **not** implement all HLS features (AES-128, DRM, DASH, complex rendition or byte-range handling). Some providers may require special headers, cookies or stream-specific handling. A valid network response is **not** guaranteed to yield a decodable JPEG.

## Security and privacy

- **No private playlists, stream URLs, usernames, tokens, LAN addresses or local directory paths** are included in this repository.
- The `/status` endpoint can reveal channel names and error details; do not publish it on the internet.
- Stream URLs and channel IDs can be sensitive; keep your actual playlist outside version control.
- The server accepts source names **only** from the administrator-supplied local playlist; it does not accept arbitrary URLs via browser requests.
- This code still needs broader validation, concurrency/load tests, secure deployment, and upstream review before production use.

## Contributing / upstream

See [Architecture and proposal](docs/ARCHITECTURE.md). To suggest a Jellyfin integration, begin with a technical discussion in the [Jellyfin community](https://github.com/orgs/jellyfin/discussions). This repository is **not** a pull request against Jellyfin Web.

## Credit

The multi-session asynchronous I/O and separate decoder-pool architecture was proposed by **Andrea**. The prototype implementation and documentation were developed collaboratively with ChatGPT. Original community work, not an official Jellyfin release.

## License

MIT — see [LICENSE](LICENSE). Jellyfin itself has its own licenses and trademarks; this project is not affiliated with the Jellyfin team.
