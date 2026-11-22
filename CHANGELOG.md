# Changelog

## 2026-11-22 — ZAPPING push / split architecture (experimental snapshot)

- Replaced the old prototype’s small shared decoder pool with one independent per-channel worker OS process for each requested unique channel.
- Replaced client-side periodic JPEG pulling with persistent WebSocket subscription and push distribution by Favorites.
- Introduced strict responsibility split: ZAPPING-JELLYFIN (clients/queues) vs ZAPPING-PROVIDERS (HLS/FFmpeg).
- Added per-client pending-frame replacement, browser-side last-frame persistence and fixed-position restart menu.
- Added classified placeholder images for provider/decode errors.
- Added HLS master variant selection, redirect-relative URL resolution, standard AES-128 key processing (not DRM), route fallback/cooldowns and persistent SQLite route preferences.
- Included the corresponding architecture, route, redirect and AES-128 simulated tests.
- Removed deployment-specific private IP addresses and personal playlist path **from the GitHub publication only**. The user’s installed service is not automatically modified by this archive.

### Known limitations

- No secure authenticated remote access; restrict endpoints to trusted LAN/authorized reverse proxy.
- Web interface integration uses Jellyfin Web internal/DOM APIs and requires manual script injection.
- Source resolution is limited to playlist entries, HLS variants and known URL-specific resolvers; not a universal stream finder.
- Some streams need more than the 1-second FFmpeg default. Some HLS formats (DRM, SAMPLE-AES, complex renditions) are unsupported.
- File-based/SQLite state must remain private and is deliberately excluded.
