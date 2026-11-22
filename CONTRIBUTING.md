# Contributing

Community / experimental code, not an official Jellyfin component.

- Keep the responsibility split intact: `zapping_jellyfin` is for sessions, per-channel workers, queues and WebSocket; `zapping_providers` is for source resolution, HLS and FFmpeg.
- Follow existing Python **snake_case** and JavaScript **camelCase** naming; do not rename established identifiers without a reason.
- Never commit real IPTV playlists, user URLs, API keys, cookies, passwords, session material, SQLite runtime databases, logs, caches or local backups.
- Treat a channel that times out or produces a decoder error independently from other channels; avoid making global queues block.
- Add or adapt a local/simulated test for any change to route selection, AES-128 processing, relinker behavior or WebSocket delivery.
- Report tested Python/Jellyfin/FFmpeg versions explicitly. Do not assert broad compatibility based on one installation.
- Review [Security](docs/SECURITY.md) before proposing network exposure or authentication changes.
