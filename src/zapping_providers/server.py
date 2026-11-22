#!/usr/bin/env python3
"""ZAPPING-PROVIDERS: solo playlist, HLS e FFmpeg; NON conosce utenti/Preferiti.

GET /frame?name=... restituisce UN JPEG o un errore tipizzato:
  204: segmento invariato, nessun nuovo fotogramma
  422 {error: decode_error}: timeout/errore di decodifica
  504 {error: provider_timeout}: timeout di acquisizione HLS
  502 {error: provider_error}: altra indisponibilita' della sorgente
"""
import argparse
import asyncio
import hashlib
import shutil
import json
import re
import shlex
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
from aiohttp import web

try:
    from .route_store import RouteStore
except ImportError:
    from route_store import RouteStore


class DecodeError(Exception):
    pass


class ProviderError(Exception):
    pass


class ProviderServer:
    def __init__(self, playlist_path, ffmpeg_command, decode_timeout):
        self.playlist_path = Path(playlist_path) if playlist_path else None
        self.ffmpeg_command = ffmpeg_command
        self.decode_timeout = decode_timeout
        self.channels = {}
        self.playlist_mtime = 0
        self.last_segment = {}
        self.session = None
        self.processes = set()
        self.request_count = 0
        self.decoded_count = 0
        self.errors = {}
        self.active_decoders = set()
        self.locks = {}
        self.route_store = RouteStore(Path(__file__).resolve().parent / 'state' / 'sources.sqlite3')
        self.route_cooldowns = {}
        self.media_cooldowns = {}
        self.source_routes = {}
        self.rai_routes = {}
        self.keys_cache = {}
        self.load_playlist()

    def load_playlist(self):
        if self.playlist_path is None or not self.playlist_path.is_file():
            return
        mtime = self.playlist_path.stat().st_mtime_ns
        if mtime == self.playlist_mtime:
            return
        channels = {}
        aliases = {}
        current_name = None
        for raw in self.playlist_path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            line = raw.strip()
            if line.startswith('#EXTINF:'):
                current_name = line.rsplit(',', 1)[-1].strip()
            elif not line or line.startswith('#'):
                continue
            elif current_name:
                if urlsplit(line).scheme in ('http', 'https'):
                    channels.setdefault(current_name, []).append(line)
                    alias = re.sub(r'^\d+\s+', '', current_name)
                    if alias != current_name:
                        aliases.setdefault(alias, set()).add(current_name)
                current_name = None
        for alias, originals in aliases.items():
            if alias not in channels and len(originals) == 1:
                original = next(iter(originals))
                if len(channels[original]) == 1:
                    channels[alias] = channels[original]
        self.channels = channels
        self.playlist_mtime = mtime
        self.last_segment.clear()
        self.route_cooldowns.clear()
        self.media_cooldowns.clear()
        self.source_routes.clear()
        self.rai_routes.clear()
        self.keys_cache.clear()

    async def start(self, app):
        timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=6)
        connector = aiohttp.TCPConnector(limit=0, limit_per_host=0, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def stop(self, app):
        for process in list(self.processes):
            if process.returncode is None:
                process.kill()
        for process in list(self.processes):
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        if self.session:
            await self.session.close()
        self.route_store.close()

    async def fetch_data(self, url, agent, binary=False, with_final_url=False):
        headers = {'User-Agent': agent, 'Accept': '*/*'}
        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > (12000000 if binary else 1500000):
                raise ProviderError('Dati HLS troppo grandi')
            payload = await response.read()
            if len(payload) > (12000000 if binary else 1500000):
                raise ProviderError('Risposta HLS troppo grande')
            content = payload if binary else payload.decode('utf-8-sig', 'replace')
            if with_final_url:
                return content, str(response.url)
            return content

    @staticmethod
    def hls_attributes(line):
        """Legge gli attributi HLS, inclusi valori URI tra virgolette."""
        attributes = {}
        for match in re.finditer(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)', line.split(':', 1)[-1]):
            attributes[match.group(1).upper()] = match.group(2).strip('"')
        return attributes

    async def hls_key(self, key_url, agent):
        """Scarica una chiave HLS AES-128 pubblicata dalla stessa sorgente.

        Cache breve solo in RAM: chiavi mai scritte in SQLite o nei log.
        """
        now = time.monotonic()
        key_identity = (key_url, agent)
        cached = self.keys_cache.get(key_identity)
        if cached and cached[1] > now:
            return cached[0]
        key = await self.fetch_data(key_url, agent, binary=True)
        if len(key) != 16:
            raise ProviderError('Chiave HLS AES-128 non valida')
        if len(self.keys_cache) >= 128:
            self.keys_cache = {identity: value for identity, value in self.keys_cache.items()
                               if value[1] > now}
            if len(self.keys_cache) >= 128:
                self.keys_cache.clear()
        self.keys_cache[key_identity] = (key, now + 60)
        return key

    async def decrypt_hls(self, data, encryption, sequence, agent):
        """Decifra segmenti standard HLS METHOD=AES-128, non DRM/SAMPLE-AES."""
        if encryption is None:
            return data
        key_url, iv_hex = encryption
        key = await self.hls_key(key_url, agent)
        if iv_hex:
            try:
                iv_text = iv_hex[2:] if iv_hex.lower().startswith('0x') else iv_hex
                iv_number = int(iv_text, 16)
                iv = iv_number.to_bytes(16, 'big')
            except (ValueError, OverflowError) as exception:
                raise ProviderError('IV HLS AES-128 non valido') from exception
        else:
            # RFC 8216: IV implicito = MEDIA-SEQUENCE in 128 bit big-endian.
            try:
                iv = sequence.to_bytes(16, 'big')
            except (ValueError, OverflowError) as exception:
                raise ProviderError('MEDIA-SEQUENCE HLS non valido') from exception
        if not data or len(data) % 16:
            raise ProviderError('Segmento HLS AES-128 non valido')
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            padded = decryptor.update(data) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        except ImportError:
            # Seconda opzione senza dipendenze Python: OpenSSL di sistema.
            if not shutil.which('openssl'):
                raise ProviderError('AES-128 HLS: servono cryptography o openssl')
            process = await asyncio.create_subprocess_exec(
                'openssl', 'enc', '-d', '-aes-128-cbc', '-K', key.hex(), '-iv', iv.hex(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, error = await asyncio.wait_for(process.communicate(data), timeout=2)
            except asyncio.TimeoutError as exception:
                process.kill()
                await process.wait()
                raise ProviderError('Timeout decrittazione AES-128 HLS') from exception
            if process.returncode != 0:
                raise ProviderError('Decrittazione AES-128 HLS fallita')
            return output
        except ValueError as exception:
            raise ProviderError('Decrittazione AES-128 HLS fallita') from exception

    async def hls_segment(self, url, agent, name=None):
        """Risolve varianti HLS, redirect, URL e chiavi AES-128 dei segmenti.

        Nessun controllo sul nome del canale e nessuna gestione di DRM.
        """
        visited = set()

        async def inspect_playlist(playlist_url, depth):
            if depth >= 4 or playlist_url in visited:
                raise ProviderError('Playlist HLS ricorsiva o troppo profonda')
            visited.add(playlist_url)
            playlist, effective_url = await self.fetch_data(
                playlist_url, agent, with_final_url=True)
            if not playlist.lstrip().startswith('#EXTM3U'):
                raise ProviderError('Risposta non HLS')
            lines = [line.strip() for line in playlist.splitlines() if line.strip()]
            variants = []
            for index, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF:') and index + 1 < len(lines):
                    if lines[index + 1].startswith('#'):
                        continue
                    bandwidth = 0
                    for field in line.split(':', 1)[1].split(','):
                        if field.startswith('BANDWIDTH='):
                            try:
                                bandwidth = int(field.split('=', 1)[1])
                            except ValueError:
                                pass
                    has_video = ('RESOLUTION=' in line.upper() or
                                 re.search(r'(avc1|avc3|hvc1|hev1|av01|vp09|dvh1|dvhe)',
                                           line, re.IGNORECASE) is not None)
                    variants.append((0 if has_video else 1, bandwidth,
                                     urljoin(effective_url, lines[index + 1])))
            if variants:
                last_exception = None
                ordered = sorted(variants)
                if name is not None:
                    now = time.monotonic()
                    ready = [variant for variant in ordered
                             if self.media_cooldowns.get((name, variant[2]), 0) <= now]
                    if ready:
                        ordered = ready
                for _, _, variant_url in ordered:
                    try:
                        return await inspect_playlist(variant_url, depth + 1)
                    except (ProviderError, aiohttp.ClientError, asyncio.TimeoutError) as exception:
                        last_exception = exception
                        if name is not None:
                            self.media_cooldowns[(name, variant_url)] = time.monotonic() + 30
                if last_exception is not None:
                    raise last_exception
                raise ProviderError('Nessuna variante HLS utilizzabile')

            media_sequence = 0
            encryption = None
            init_info = None
            segment_info = []
            for line in lines:
                if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                    try:
                        media_sequence = int(line.split(':', 1)[1])
                    except ValueError as exception:
                        raise ProviderError('MEDIA-SEQUENCE HLS non valido') from exception
                elif line.startswith('#EXT-X-KEY:'):
                    attributes = self.hls_attributes(line)
                    method = attributes.get('METHOD', '').upper()
                    if method == 'NONE':
                        encryption = None
                    elif method == 'AES-128' and attributes.get('URI') and attributes.get('KEYFORMAT', 'identity').lower() == 'identity':
                        encryption = (urljoin(effective_url, attributes['URI']), attributes.get('IV'))
                    else:
                        raise ProviderError('HLS cifrato: metodo ' + (method or 'sconosciuto') + ' non supportato')
                elif line.startswith('#EXT-X-MAP:'):
                    attributes = self.hls_attributes(line)
                    if attributes.get('URI'):
                        init_info = (urljoin(effective_url, attributes['URI']), encryption, media_sequence)
                elif not line.startswith('#'):
                    segment_info.append((urljoin(effective_url, line), encryption, media_sequence))
                    media_sequence += 1
            if not segment_info:
                raise ProviderError('Nessun segmento HLS')
            return segment_info[-1], init_info, effective_url

        return await inspect_playlist(url, 0)

    async def decode(self, name, segment):
        process = None
        self.active_decoders.add(name)
        try:
            command = self.ffmpeg_command + [
                '-hide_banner', '-nostdin', '-loglevel', 'error',
                '-i', 'pipe:0', '-map', '0:v:0', '-an', '-sn', '-dn',
                '-frames:v', '1', '-vf', 'scale=426:-2', '-q:v', '7',
                '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'
            ]
            async def run_decoder():
                nonlocal process
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                self.processes.add(process)
                output, error = await process.communicate(segment)
                return output, error

            try:
                output, error = await asyncio.wait_for(run_decoder(), timeout=self.decode_timeout)
            except asyncio.TimeoutError as exception:
                raise DecodeError('Timeout FFmpeg ' + str(self.decode_timeout) + ' s') from exception
            if process.returncode or not output.startswith(b'\xff\xd8') or not output.endswith(b'\xff\xd9'):
                raise DecodeError('Decoder: ' + error.decode('utf-8', 'replace')[-180:])
            return output
        finally:
            if process:
                if process.returncode is None:
                    process.kill()
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=1.5)
                    except (asyncio.TimeoutError, RuntimeError, ValueError):
                        pass
                self.processes.discard(process)
            self.active_decoders.discard(name)

    @staticmethod
    def rai_candidates(url):
        """Riprova solo il relinker Rai, mantenendo identico il codice cont.

        Non sostituisce la sorgente e non modifica la playlist Jellyfin.
        """
        parts = urlsplit(url)
        if (parts.hostname or '').lower() != 'mediapolis.rai.it' or '/relinker/' not in parts.path.lower():
            agent = 'rainet/4.0.5' if 'mediapolis.rai.it' in (parts.netloc or '').lower() else 'Mozilla/5.0'
            return [(url, agent, 'playlist')]
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        if not params.get('cont'):
            return [(url, 'rainet/4.0.5', 'playlist')]
        variants = [(url, 'rainet/4.0.5', 'playlist')]
        for output, force_agent, agent, label in (
            ('16', None, 'rainet/4.0.5', 'relinker output=16'),
            ('7', 'raiplayappletv', 'raiplayappletv', 'relinker raiplayappletv'),
        ):
            variant_params = dict(params)
            variant_params['output'] = output
            if force_agent is None:
                variant_params.pop('forceUserAgent', None)
            else:
                variant_params['forceUserAgent'] = force_agent
            variant_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                      urlencode(variant_params), parts.fragment))
            if variant_url != url:
                variants.append((variant_url, agent, label))
        return variants

    @staticmethod
    def playlist_digest(sources):
        # La preferenza va scartata se cambiano gli URL della playlist.
        data = json.dumps(sources, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def source_candidates(self, sources):
        """Alternativi espliciti nella M3U + risolutori applicabili all'URL, mai al nome."""
        candidates = []
        seen = set()
        for index, url in enumerate(sources):
            for variant_url, agent, label in self.rai_candidates(url):
                identity = (variant_url, agent)
                if identity not in seen:
                    seen.add(identity)
                    description = f'playlist #{index + 1}' if label == 'playlist' else label
                    candidates.append((variant_url, agent, description))
        return candidates

    async def acquire_segment(self, name, variant_url, agent, resolved_url=None):
        async def from_playlist(playlist_url):
            segment_info, init_info, used_url = await self.hls_segment(playlist_url, agent, name=name)
            segment_url, encryption, media_sequence = segment_info
            if self.last_segment.get(name) == segment_url:
                return segment_url, None, used_url
            segment = await self.fetch_data(segment_url, agent, binary=True)
            segment = await self.decrypt_hls(segment, encryption, media_sequence, agent)
            if init_info:
                init_url, init_encryption, init_sequence = init_info
                init_data = await self.fetch_data(init_url, agent, binary=True)
                init_data = await self.decrypt_hls(init_data, init_encryption, init_sequence, agent)
                segment = init_data + segment
            return segment_url, segment, used_url

        # Una variante gia' decodificata viene riutilizzata direttamente.
        # Se e' scaduta o non esiste piu', torniamo alla sorgente master.
        if (resolved_url and resolved_url != variant_url
                and self.media_cooldowns.get((name, resolved_url), 0) <= time.monotonic()):
            try:
                return await asyncio.wait_for(from_playlist(resolved_url), timeout=2.5)
            except (asyncio.TimeoutError, ProviderError, aiohttp.ClientError, OSError):
                pass
        return await from_playlist(variant_url)

    async def get_frame(self, name):
        self.request_count += 1
        self.load_playlist()
        sources = list(dict.fromkeys(self.channels.get(name, [])))
        if not sources:
            raise ProviderError('Sorgente IPTV non risolta')
        async with self.locks.setdefault(name, asyncio.Lock()):
            playlist_digest = self.playlist_digest(sources)
            candidates = self.source_candidates(sources)
            preferred = self.route_store.get(name, playlist_digest)
            preferred_route = preferred[:3] if preferred else None
            if preferred_route in candidates:
                candidates.remove(preferred_route)
                candidates.insert(0, preferred_route)
            # Le sorgenti fallite non devono essere riprovate ogni 3 secondi.
            # Ogni canale resta comunque indipendente dagli altri.
            now = time.monotonic()
            ready = [route for route in candidates if self.route_cooldowns.get((name, route[:2]), 0) <= now]
            if not ready:
                ready = candidates[:1]
            last_exception = None
            for variant_url, agent, label in ready:
                route_key = (name, (variant_url, agent))
                try:
                    resolved_url = None
                    cached_url = preferred[3] if preferred and preferred_route == (variant_url, agent, label) else None
                    segment_url, segment, resolved_url = await self.acquire_segment(
                        name, variant_url, agent, cached_url)
                    if segment is None:
                        return None
                    jpeg = await self.decode(name, segment)
                    # Memorizza soltanto sorgenti che hanno prodotto un JPEG valido.
                    self.last_segment[name] = segment_url
                    self.decoded_count += 1
                    self.errors.pop(name, None)
                    self.route_cooldowns.pop(route_key, None)
                    self.source_routes[name] = label
                    if label.startswith('relinker'):
                        self.rai_routes[name] = label  # Diagnostica compatibile con patch precedente.
                    else:
                        self.rai_routes.pop(name, None)
                    try:
                        self.route_store.save(name, playlist_digest, variant_url, agent, label, resolved_url)
                    except (OSError, sqlite3.Error) as exception:
                        print('ZAPPING-PROVIDERS: scrittura preferenza fallita:', exception, flush=True)
                    return jpeg
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exception:
                    last_exception = TimeoutError('Timeout acquisizione HLS')
                except DecodeError as exception:
                    last_exception = exception
                    if resolved_url:
                        self.media_cooldowns[(name, resolved_url)] = time.monotonic() + 30
                except (ProviderError, aiohttp.ClientError, OSError, ValueError) as exception:
                    last_exception = exception
                self.route_cooldowns[route_key] = time.monotonic() + 20
            if last_exception is not None:
                raise last_exception
            raise ProviderError('Nessuna sorgente disponibile')


def json_reply(data, status=200):
    return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})


async def frame_handler(request):
    provider = request.app['provider']
    name = request.query.get('name', '')
    if not name or len(name) > 150:
        return json_reply({'error': 'provider_error', 'detail': 'Nome canale non valido'}, 400)
    try:
        jpeg = await provider.get_frame(name)
        if jpeg is None:
            return web.Response(status=204, headers={'Cache-Control': 'no-store'})
        return web.Response(body=jpeg, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})
    except DecodeError as exception:
        provider.errors[name] = 'decode_error: ' + str(exception)[:180]
        return json_reply({'error': 'decode_error', 'detail': str(exception)[:180]}, 422)
    except (TimeoutError, asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exception:
        provider.errors[name] = 'provider_timeout: ' + str(exception)[:180]
        return json_reply({'error': 'provider_timeout', 'detail': str(exception)[:180]}, 504)
    except (ProviderError, aiohttp.ClientError, OSError, ValueError) as exception:
        provider.errors[name] = 'provider_error: ' + str(exception)[:180]
        return json_reply({'error': 'provider_error', 'detail': str(exception)[:180]}, 502)


async def status_handler(request):
    provider = request.app['provider']
    return json_reply({'service': 'online', 'type': 'zapping-providers',
                       'channels_catalog': len(provider.channels),
                       'requests': provider.request_count,
                       'decoded_frames': provider.decoded_count,
                       'active_decoders': sorted(provider.active_decoders),
                       'rai_routes': dict(provider.rai_routes),
                       'preferred_routes': dict(provider.source_routes),
                       'saved_routes': provider.route_store.count(),
                       'errors': dict(provider.errors)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8110)
    parser.add_argument('--playlist', default=None)
    parser.add_argument('--ffmpeg', default='docker exec -i jellyfin /usr/lib/jellyfin-ffmpeg/ffmpeg')
    parser.add_argument('--decode-timeout', type=float, default=1.0)
    args = parser.parse_args()
    if not 0 < args.decode_timeout <= 60:
        parser.error('Timeout decoder non valido')
    root = Path(__file__).resolve().parent.parent
    playlist_path = Path(args.playlist).expanduser() if args.playlist else None
    if playlist_path is None:
        candidates = [root / item for item in ('playlist.m3u', 'playlist.m3u8', 'channels.m3u', 'channels.m3u8')]
        candidates.extend(sorted(root.glob('*.m3u')))
        candidates.extend(sorted(root.glob('*.m3u8')))
        playlist_path = next((path for path in candidates if path.is_file()), None)
    if playlist_path is None or not playlist_path.is_file():
        parser.error('Playlist IPTV non trovata: specificare --playlist')
    provider = ProviderServer(playlist_path, shlex.split(args.ffmpeg), args.decode_timeout)
    app = web.Application()
    app['provider'] = provider
    app.on_startup.append(provider.start)
    app.on_cleanup.append(provider.stop)
    app.router.add_get('/frame', frame_handler)
    app.router.add_get('/status', status_handler)
    print('ZAPPING-PROVIDERS', args.host, args.port, 'timeout decoder:', args.decode_timeout, flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None, handle_signals=True)


if __name__ == '__main__':
    main()
