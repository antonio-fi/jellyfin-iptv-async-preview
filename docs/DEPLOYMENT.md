# Manual deployment and rollback (advanced / experimental)

**Read [Security](SECURITY.md) first.** The bundle is a reproducible GitHub source snapshot, not an automated installer and not a seamless update of a running system. Back up the existing working service and Jellyfin web assets before changes.

## Prerequisites and Python install

- Linux with Python 3.10+, `fuser` (`psmisc`) and FFmpeg (host binary or Docker command).
- Jellyfin Web with compatible **Live TV → On Now** programme cards.
- Your **own private** M3U/M3U8 file. Never copy it into GitHub.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Either optional Python cryptography or the host's openssl for AES-128:
# pip install -r requirements-aes128.txt
```

To start manually (replace address, paths, and FFmpeg command):

```bash
python src/gestore_iptv.py \
    --host 192.168.1.20 \
    --origin http://192.168.1.20:8096 \
    --playlist /private/path/channels.m3u8 \
    --ffmpeg ffmpeg \
    --decode-timeout 1 \
    --response-timeout 10 \
    --sleep 3
```

The supervisor launches `src/server_auto.py` (Jellyfin front end on `:8099`) and `src/zapping_providers/server.py` (backend on `127.0.0.1:8110`). On Linux, `fuser` is needed by the supervisor for port ownership checks. The manager API is on `:8100`. If FFmpeg is installed only in the Jellyfin Docker container, use `--ffmpeg 'docker exec -i jellyfin /usr/lib/jellyfin-ffmpeg/ffmpeg'` instead.

**Compatibility notice:** `--workers` and `--cache` are retained in some CLI entry points to support old launch commands, but the push version does **not** use a shared FFmpeg pool or disk JPEG cache. Each distinct active Favorite is assigned an independent worker.

## Embed JavaScript into Jellyfin Web

**Stop and back up first.** Identify the actual web root inside your container:

```bash
docker exec jellyfin sh -c 'for d in /jellyfin/jellyfin-web /usr/share/jellyfin/web /jellyfin-web; do test -f "$d/index.html" && echo "$d"; done'
```

For example, if the result is `/jellyfin/jellyfin-web`, on the host:

```bash
mkdir -p ./jellyfin-web-backup
docker cp jellyfin:/jellyfin/jellyfin-web/index.html ./jellyfin-web-backup/index.html
docker cp src/tv_firenze_ora_auto.js jellyfin:/jellyfin/jellyfin-web/tv_firenze_ora_auto.js
```

Edit a **copy** of your backed-up `index.html` on the host and add this line immediately before `</head>` (only once):

```html
<script defer src="/web/tv_firenze_ora_auto.js"></script>
```

Then deploy the edited HTML, for example:

```bash
cp ./jellyfin-web-backup/index.html ./jellyfin-web-backup/index.modified.html
# Edit ./jellyfin-web-backup/index.modified.html with your editor now.
docker cp ./jellyfin-web-backup/index.modified.html jellyfin:/jellyfin/jellyfin-web/index.html
```

**Verify that you have actually inserted the script** before copying; do not run the commands blindly. If your installation already loads `tv_firenze_ora_auto.js`, do not add a duplicate HTML tag. Container/image updates may overwrite manual changes.

### Browser URL configuration

In this public version, the JS defaults to the **current Jellyfin page hostname** on ports `8100` (HTTP) and `8099` (WebSocket). For a separate host or HTTPS reverse proxy, define these globals **before** loading the JS:

```html
<script>
window.jellyfinZappingManagerBase = 'https://your-approved-proxy.example/zapping-manager';
window.jellyfinZappingSocket = 'wss://your-approved-proxy.example/zapping/ws';
</script>
<script defer src="/web/tv_firenze_ora_auto.js"></script>
```

Your reverse proxy must route these paths to the corresponding ports and correctly forward WebSocket upgrades; use an authenticated proxy when leaving a trusted LAN. A mismatched `--origin`/CORS check can prevent the browser from connecting. Hard-refresh the browser (`Ctrl+Shift+R`) after JS changes.

## Optional systemd

See [`examples/jellyfin-zapping.service.example`](../examples/jellyfin-zapping.service.example). **Edit all placeholders and verify service user/group** before enabling. Do not keep two copies of the supervisor running simultaneously. If an existing systemd service already works, keep that service definition and simply point it to the new, backed-up code only after tests.

## Status and rollback

With the On Now page open, compare readings from:

```bash
curl -fsS http://192.168.1.20:8100/status
curl -fsS http://192.168.1.20:8099/status
curl -fsS http://127.0.0.1:8110/status
```

To undo browser integration, restore `index.html` from the backup via `docker cp`, remove the JS reference and refresh. To undo server deployment, stop the new service and restore the previously backed-up source/venv and service configuration. Source-route SQLite contains runtime state and is not a substitute for a full backup.

**Do not upload the user’s production installation directly to GitHub.** Use the clean source bundle and review `git status` + staged changes before pushing.
