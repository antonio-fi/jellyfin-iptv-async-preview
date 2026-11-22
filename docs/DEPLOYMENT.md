# Manual Docker deployment (advanced / experimental)

Jellyfin Web is a single-page app. Its `index.html` and JavaScript assets inside a container can be overwritten by image updates. Make **a backup** before testing and keep a simple rollback path.

These commands are examples only. Replace container names and paths according to your installation. Never copy an entire private IPTV playlist into this repository.

## 1. Locate the web directory

```bash
docker exec jellyfin sh -c 'for d in /jellyfin/jellyfin-web /usr/share/jellyfin/web /jellyfin-web /usr/lib/jellyfin/bin/jellyfin-web; do if test -f "$d/index.html"; then echo "$d"; fi; done'
```

Choose one matching directory and set its value in the shell as `WEB_DIR` (for example `WEB_DIR=/jellyfin/jellyfin-web`).

## 2. Backup and install only the browser integration

```bash
mkdir -p ./jellyfin-web-backup
docker cp "jellyfin:$WEB_DIR/index.html" ./jellyfin-web-backup/index.html
cp ./jellyfin-web-backup/index.html ./jellyfin-web-backup/index.modified.html
```

Edit `./jellyfin-web-backup/index.modified.html` in a text editor and add this script line immediately before `</head>`:

```html
<script defer src="/web/tv_firenze_ora_auto.js"></script>
```

Do not add it twice. If another custom preview script already exists, disable that script's HTML tag rather than running two conflicting preview systems.

```bash
docker cp src/tv_firenze_ora_auto.js "jellyfin:$WEB_DIR/tv_firenze_ora_auto.js"
docker cp ./jellyfin-web-backup/index.modified.html "jellyfin:$WEB_DIR/index.html"
```

The service runs separately from Jellyfin, as shown in the README. **Do not restart Jellyfin solely to load the changed JS**; hard-refresh the browser instead.

## 3. Roll back

```bash
docker cp ./jellyfin-web-backup/index.html "jellyfin:$WEB_DIR/index.html"
```

Stop the external preview service if you started it. The extra JS file can be left unused or removed deliberately.

## Restrictions

- If your Jellyfin interface is served over HTTPS, the preview server must be presented through HTTPS as well (e.g. authenticated reverse proxy).
- Jellyfin Web DOM can change on updates. This is not a supported plugin interface.
- The prototype's browser script uses `ApiClient.getLiveTvChannels({IsFavorite:true})`, matching the original local test environment. General setups and favourites filtering can behave differently.
- Protect `8099` from untrusted clients; CORS is not sufficient.
