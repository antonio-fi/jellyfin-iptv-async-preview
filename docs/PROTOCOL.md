# Protocol and diagnostics

## Browser → ZAPPING-JELLYFIN (:8099)

Connect `ws://SERVER:8099/ws` (or `wss://...` behind a correctly configured HTTPS proxy). Browser sends UTF-8 JSON:

```json
{"type":"subscribe","client_id":"browser-generated-id","names":["Rai 1","Canale 5"]}
```

Server sends a JSON acknowledgement `{ "type": "subscribed", "channels": 2 }`. For each new frame the server sends one **binary WebSocket message**:

```text
UTF-8 channel name | 0x00 separator | JPEG binary bytes
```

No repeated client-side HTTP `/frame` requests are needed. A new subscription supersedes the previous name set on that socket. The `client_id` value is **not** an authentication credential.

### Endpoints

| Address | Method | Purpose |
| --- | --- | --- |
| `:8099/ws` | WS | Subscribe and receive JPEGs automatically. |
| `:8099/status` | GET | Session/channel counts, worker PIDs, queue size, errors. |
| `:8099/clients` | GET | Client IDs and subscription counts. |
| `:8099/state`, `:8099/frame` | GET | Compatibility endpoints (not used by push JS). |
| `:8100/status` | GET | Aggregated supervisor status. |
| `:8100/admin/restart` | POST | Restart ZAPPING services. **Protect this endpoint.** |
| `:8100/admin/start`, `stop`, `clear-sessions` | POST | Pass-through admin controls. |
| `127.0.0.1:8110/frame?name=...` | GET | Provider returns one JPEG or typed error; internal only. |
| `127.0.0.1:8110/status` | GET | HLS acquisition counters, active decoders, source preferences, errors. |

The Provider `/frame` outcomes are `200 image/jpeg`, `204 no new segment`, `422 decode_error`, `502 provider_error`, `504 provider_timeout`. The channel worker converts errors into bundled diagnostic JPEGs for transmission. Detailed provider error messages remain visible in `/status` but **are not painted onto the current placeholder image**.

### Timeouts

The provider's `aiohttp.ClientTimeout(total=8,connect=3,sock_read=6)` bounds **individual HTTP requests**, not a complete sequence of nested playlist + media segment + key reads. Channel worker `urlopen(..., timeout=10)` is a socket timeout, not a strict end-to-end cap. FFmpeg is independently bounded by `--decode-timeout` (default 1 second).

### Live checks

With a local trusted shell session (replace `SERVER` with the host):

```bash
curl -fsS http://SERVER:8100/status | python3 -m json.tool
curl -fsS http://SERVER:8099/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8110/status | python3 -m json.tool
```

A counter is normally **cumulative since process start**. Compare two readings rather than treating a nonzero total as evidence that frames are currently being produced. `errors` is the provider's latest stored status per channel, not a global health verdict for all available streams.

### Typical diagnostics

- `clients: 0` while the page is open: check injected JS, browser console, WebSocket URL/origin, TLS/mixed-content and tab visibility.
- `channels > 0` with no worker processes: inspect the session manager process logs and worker startup.
- `provider_timeout`: inspect slow/unresponsive HLS HTTP requests; do not assume FFmpeg is at fault.
- `decode_error`: FFmpeg returned no valid JPEG or exceeded `--decode-timeout`.
- A JPEG placeholder on one source while other sources work is expected; sources are independent but machine resources are shared.
- A returned last frame on a card does not necessarily prove the latest acquisition succeeded: client/server can preserve the previous frame.

Logs created by supervisor: `src/server_auto.log` and `src/zapping_providers.log`; they are **excluded by .gitignore**. They can contain private source or error metadata; redact before sharing.
