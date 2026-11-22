# Security and privacy

**Not suitable for direct public-Internet exposure.** This is a LAN research prototype.

1. **Authentication:** ZAPPING does **not** verify Jellyfin login tokens in its own WebSocket or HTTP services. A browser-provided `client_id` is an identifier, not proof of identity. The `Origin` header check only mitigates some browser-based cross-origin access and can be forged by non-browser clients.
2. **Administrative actions:** `:8100/admin/restart` and other controls can affect running services. The current code checks an Origin header, **not a password/token**. Do not allow untrusted hosts to reach this API.
3. **Network exposure:** Keep the provider on `127.0.0.1:8110`. Restrict `8099` and `8100` with a host firewall and/or an authenticated reverse proxy. HTTPS Jellyfin pages need WSS and HTTPS for ZAPPING traffic.
4. **Stream access rights:** use only channels and HLS decryption keys you are authorized to access. Standard HLS AES-128 key handling is not DRM bypass. Do not attempt to circumvent access protections.
5. **Private content:** never commit `.m3u`/`.m3u8` playlists, SQLite files, secrets, keys, logs, cache, `*.env`, credentials, cookies or configuration containing authenticated URLs. A stored source URL can itself contain access tokens.
6. **Resource use:** each unique active favorite has its own process and can request FFmpeg. Apply OS/network resource restrictions if using outside a controlled test LAN. No global hard worker cap is enforced in this snapshot.
7. **Application coupling:** injection into Jellyfin Web assets may break with upgrades or different container layouts. Keep backups and test restores.

## Before publishing

```bash
git status --short
git diff --cached --name-only
git grep -nE 'token|api[_-]?key|password|secret|10\.[0-9]+\.[0-9]+\.[0-9]+' -- ':!docs/SECURITY.md'
```

These are **heuristics**, not a guarantee. Inspect all files to be committed, including images/archives and git history. If credentials were previously committed, removing them from a new commit does not erase them from the git history; rotate and handle the history separately.
