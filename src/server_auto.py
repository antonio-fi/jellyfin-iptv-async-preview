#!/usr/bin/env python3
"""Jellyfin Live TV: acquisizione JPEG concorrente e prioritizzata, senza player Jellyfin.
Compatibile con gli endpoint del servizio precedente sulla porta 8099.
"""
import argparse
import aiohttp
from collections import deque
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, urljoin


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, playlist_path, ffmpeg_command, cache_dir, workers, allowed_origin):
        super().__init__(address, handler)
        self.playlist_path = Path(playlist_path)
        self.ffmpeg_command = ffmpeg_command
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_origin = allowed_origin
        self.lock = threading.RLock()
        self.channels = {}
        self.playlist_mtime = 0
        self.active_names = []
        self.last_heartbeat = 0.0
        self.errors = {}
        self.attempts = {}
        self.updating = set()
        self.focused_name = ''
        self.frame_buffers = {}
        self.pending = {}
        self.sequence = 0
        self.workers = workers
        self.loop = asyncio.new_event_loop()
        self.queue = None
        self.wakeup = None
        self.receivers = {}
        self.decoder_limit = None
        self.session = None
        self.last_segment = {}
        self.request_count = 0
        self.decoded_count = 0
        self.focus_changed = None
        self.tasks = []
        self.processes = set()
        self.load_playlist()
        self.loop_thread = threading.Thread(target=self.run_loop, daemon=True)
        self.loop_thread.start()

    def load_playlist(self):
        mtime = self.playlist_path.stat().st_mtime_ns
        if mtime == self.playlist_mtime:
            return
        channels = {}
        current_name = None
        for raw in self.playlist_path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            line = raw.strip()
            if line.startswith('#EXTINF:'):
                current_name = line.rsplit(',', 1)[-1].strip()
            elif line.startswith('#') or not line:
                continue
            elif current_name:
                if urlsplit(line).scheme in ('http', 'https'):
                    channels.setdefault(current_name, []).append(line)
                current_name = None
        with self.lock:
            self.channels = channels
            self.playlist_mtime = mtime

    def frame_path(self, name):
        return self.cache_dir / (hashlib.sha256(name.encode('utf-8')).hexdigest() + '.jpg')

    def is_active(self):
        return bool(self.active_names) and time.monotonic() - self.last_heartbeat < 13

    def run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.wakeup = asyncio.Event()
        self.focus_changed = asyncio.Event()
        self.decoder_limit = asyncio.Semaphore(self.workers)
        self.tasks = [self.loop.create_task(self.session_manager()),
                      self.loop.create_task(self.focus_monitor())]
        self.loop.run_forever()

    def notify(self):
        self.loop.call_soon_threadsafe(self.wakeup.set)
        self.loop.call_soon_threadsafe(self.focus_changed.set)

    async def session_manager(self):
        # Un singolo comunicatore HTTP, molte sessioni logiche non bloccanti.
        timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=9)
        connector = aiohttp.TCPConnector(limit=80, limit_per_host=12, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as self.session:
            while True:
                with self.lock:
                    names = set(self.active_names) if self.is_active() else set()
                for name in list(self.receivers):
                    if name not in names or self.receivers[name].done():
                        task = self.receivers.pop(name)
                        if not task.done():
                            task.cancel()
                for name in names - self.receivers.keys():
                    if len(self.channels.get(name, [])) == 1:
                        self.receivers[name] = asyncio.create_task(self.channel_receiver(name))
                    else:
                        with self.lock:
                            self.errors[name] = 'Sorgente assente o ambigua nella playlist'
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=1.0)
                    self.wakeup.clear()
                except asyncio.TimeoutError:
                    pass

    async def fetch_data(self, url, agent, binary=False):
        headers = {'User-Agent': agent, 'Accept': '*/*'}
        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            response.raise_for_status()
            # Mai accumulare in memoria un segmento video illimitato.
            if binary and response.content_length and response.content_length > 12000000:
                raise RuntimeError('Segmento troppo grande')
            # Bound in-memory reads even when the server omits Content-Length.
            limit = 12000000 if binary else 1500000
            payload = await response.content.read(limit + 1)
            if len(payload) > limit:
                raise RuntimeError('Risposta troppo grande')
            with self.lock:
                self.request_count += 1
            return payload if binary else payload.decode('utf-8-sig', 'replace')

    async def hls_segment(self, url, agent):
        # Un ricevitore per canale: playlist master -> media -> ultimo segmento disponibile.
        for _ in range(3):
            playlist = await self.fetch_data(url, agent)
            if not playlist.lstrip().startswith('#EXTM3U'):
                raise RuntimeError('La risposta non è una playlist HLS')
            lines = [line.strip() for line in playlist.splitlines() if line.strip()]
            variants = []
            for index, line in enumerate(lines):
                if line.startswith('#EXT-X-STREAM-INF:') and index + 1 < len(lines):
                    bandwidth = 0
                    for field in line.split(':', 1)[1].split(','):
                        if field.startswith('BANDWIDTH='):
                            try: bandwidth = int(field.split('=', 1)[1])
                            except ValueError: pass
                    if not lines[index + 1].startswith('#'):
                        variants.append((bandwidth, urljoin(url, lines[index + 1])))
            if variants:
                # La qualità più bassa basta per una miniatura.
                url = min(variants)[1]
                continue
            if any(line.startswith('#EXT-X-KEY:') for line in lines):
                raise RuntimeError('HLS cifrato: necessario decoder FFmpeg dedicato')
            segments = [urljoin(url, line) for line in lines if not line.startswith('#')]
            if not segments:
                raise RuntimeError('Nessun segmento HLS')
            init = None
            for line in lines:
                if line.startswith('#EXT-X-MAP:'):
                    import re
                    match = re.search(r'URI="([^"]+)"', line)
                    if match:
                        init = urljoin(url, match.group(1))
                    break
            return (segments[-1], init)
        raise RuntimeError('Troppe playlist master concatenate')

    async def channel_receiver(self, name):
        # 62 coroutine in attesa di I/O non equivalgono a 62 processi FFmpeg.
        url = self.channels[name][0]
        agent = 'rainet/4.0.5' if 'mediapolis.rai.it' in urlsplit(url).netloc.lower() else 'Mozilla/5.0'
        while True:
            try:
                with self.lock:
                    if not self.is_active() or name not in self.active_names: return
                    focus = name == self.focused_name
                    last = self.attempts.get(name, 0)
                # Il canale in focus usa il suo streaming continuo dedicato.
                if not focus and time.monotonic() - last >= (35 if self.frame_path(name).exists() else 0):
                    segment_url, init_url = await self.hls_segment(url, agent)
                    # Callback asincrono: il ricevitore consegna al decoder soltanto segmenti nuovi.
                    if self.last_segment.get(name) != segment_url or not self.frame_path(name).exists():
                        segment = await self.fetch_data(segment_url, agent, binary=True)
                        if init_url:
                            segment = await self.fetch_data(init_url, agent, binary=True) + segment
                        await self.on_segment(name, segment)
                        self.last_segment[name] = segment_url
                    with self.lock:
                        self.attempts[name] = time.monotonic()
                        self.errors.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self.lock:
                    self.errors[name] = str(error)[:250]
                    self.attempts[name] = time.monotonic()
            # Sospensione cooperativa: nessun busy-wait.
            await asyncio.sleep(2 if name == self.focused_name else 8)

    async def on_segment(self, name, segment):
        # Decoder pool indipendente dal comunicatore HTTP.
        async with self.decoder_limit:
            with self.lock:
                if not self.is_active() or name not in self.active_names: return
                self.updating.add(name)
            process = None
            try:
                command = self.ffmpeg_command + [
                    '-hide_banner', '-nostdin', '-loglevel', 'error',
                    '-i', 'pipe:0', '-map', '0:v:0', '-an', '-sn', '-dn',
                    '-frames:v', '1', '-vf', 'scale=426:-2', '-q:v', '7',
                    '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
                process = await asyncio.create_subprocess_exec(*command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                with self.lock:
                    self.processes.add(process)
                try:
                    output, error = await asyncio.wait_for(process.communicate(segment), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    raise RuntimeError('Timeout decodifica 10 s')
                if process.returncode or not output.startswith(b'\xff\xd8') or not output.endswith(b'\xff\xd9'):
                    raise RuntimeError('Decoder: ' + error.decode('utf-8', 'replace')[-180:])
                self.store_frame(name, output)
                with self.lock:
                    self.decoded_count += 1
            finally:
                with self.lock:
                    self.updating.discard(name)
                    if process:
                        self.processes.discard(process)

    def store_frame(self, name, data):
        with self.lock:
            frames = self.frame_buffers.setdefault(name, deque(maxlen=8))
            frames.append((time.monotonic(), data))
            file_path = self.frame_path(name)
            temp_path = file_path.with_name(file_path.stem + '.' + str(time.monotonic_ns()) + '.tmp')
            temp_path.write_bytes(data)
            os.replace(temp_path, file_path)
            self.errors.pop(name, None)

    async def focus_monitor(self):
        while True:
            await self.focus_changed.wait()
            self.focus_changed.clear()
            with self.lock:
                name = self.focused_name if self.is_active() else ''
                urls = self.channels.get(name, [])
            if name and len(urls) == 1:
                await self.focus_stream(name, urls[0])

    async def focus_stream(self, name, url):
        command = self.ffmpeg_command + [
            '-hide_banner', '-nostdin', '-loglevel', 'error',
            '-probesize', '65536', '-analyzeduration', '1000000',
            '-rw_timeout', '8000000',
            *(['-user_agent', 'rainet/4.0.5'] if 'mediapolis.rai.it' in urlsplit(url).netloc.lower() else []),
            '-i', url, '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', 'fps=2,scale=426:-2', '-q:v', '7',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'
        ]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            with self.lock:
                self.processes.add(process)
                self.updating.add(name)
            data = bytearray()
            last_frame = time.monotonic()
            while True:
                with self.lock:
                    current = self.is_active() and self.focused_name == name
                if not current:
                    break
                if time.monotonic() - last_frame > 20:
                    with self.lock:
                        self.errors[name] = 'Nessun fotogramma ricevuto in 20 secondi'
                    break
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(16384), timeout=0.7)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 4_000_000:
                    raise RuntimeError('Buffer MJPEG eccessivo')
                while True:
                    first = data.find(b'\xff\xd8')
                    if first < 0:
                        data.clear()
                        break
                    if first:
                        del data[:first]
                    end = data.find(b'\xff\xd9', 2)
                    if end < 0:
                        break
                    frame = bytes(data[:end + 2])
                    del data[:end + 2]
                    with self.lock:
                        current = self.is_active() and self.focused_name == name
                    if current:
                        self.store_frame(name, frame)
                        last_frame = time.monotonic()
        except Exception as error:
            with self.lock:
                self.errors[name] = str(error)
        finally:
            if process:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                with self.lock:
                    self.processes.discard(process)
                    self.updating.discard(name)
            # Il focus rimane attivo: dopo un guasto non riavviare continuamente.
            with self.lock:
                still_focused = self.is_active() and self.focused_name == name
            if still_focused:
                try:
                    await asyncio.wait_for(self.focus_changed.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.focus_changed.set()

    def shutdown_processes(self):
        async def stop():
            for process in list(self.processes):
                if process.returncode is None:
                    process.kill()
            for task in list(self.receivers.values()) + self.tasks:
                task.cancel()
            await asyncio.gather(*list(self.receivers.values()), *self.tasks, return_exceptions=True)
            self.loop.stop()
        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=5)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def end_headers(self):
        if self.headers.get('Origin') == self.server.allowed_origin:
            self.send_header('Access-Control-Allow-Origin', self.server.allowed_origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def respond(self, code, data):
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        if self.headers.get('Origin') != self.server.allowed_origin:
            self.send_response(403)
        else:
            self.send_response(204)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        if self.headers.get('Origin') != self.server.allowed_origin:
            return self.respond(403, {'error': 'Origin non autorizzata'})
        path = urlsplit(self.path).path
        if path not in ('/channels', '/focus'):
            return self.respond(404, {'error': 'Endpoint sconosciuto'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 15000:
                return self.respond(413, {'error': 'Dimensione non valida'})
            data = json.loads(self.rfile.read(size))
            if path == '/channels':
                names = data.get('names', [])
                if not isinstance(names, list) or len(names) > 250:
                    return self.respond(400, {'error': 'Elenco non valido'})
                self.server.load_playlist()
                with self.server.lock:
                    valid = list(dict.fromkeys(name for name in names
                        if isinstance(name, str) and name in self.server.channels
                        and len(self.server.channels[name]) == 1))
                    self.server.active_names = valid
                    self.server.last_heartbeat = time.monotonic()
                self.server.notify()
                return self.respond(200, {'accepted': len(valid), 'requested': len(names)})
            name = data.get('name', '')
            with self.server.lock:
                valid = isinstance(name, str) and (not name or name in self.server.active_names)
                if valid:
                    self.server.focused_name = name
            if not valid:
                return self.respond(400, {'error': 'Canale non attivo'})
            self.server.notify()
            return self.respond(200, {'focused': name})
        except (ValueError, OSError, json.JSONDecodeError) as error:
            return self.respond(400, {'error': str(error)})

    def do_GET(self):
        origin = self.headers.get('Origin', '')
        if origin and origin != self.server.allowed_origin:
            return self.respond(403, {'error': 'Origin non autorizzata'})
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        if path.path == '/status':
            with self.server.lock:
                return self.respond(200, {'active': bool(self.server.is_active()),
                    'channels': len(self.server.active_names),
                    'updating': list(self.server.updating),
                    'pending': len(self.server.pending),
                    'buffer_sizes': {name: len(frames) for name, frames in self.server.frame_buffers.items() if name in self.server.active_names},
                    'workers': self.server.workers,
                    'network_sessions': len(self.server.receivers),
                    'http_responses': self.server.request_count,
                    'decoded_frames': self.server.decoded_count,
                    'focused': self.server.focused_name,
                    'errors': dict(self.server.errors)})
        if path.path == '/state':
            with self.server.lock:
                frames = {}
                for name in self.server.active_names:
                    file_path = self.server.frame_path(name)
                    if file_path.exists():
                        frames[name] = file_path.stat().st_mtime_ns
            return self.respond(200, {'frames': frames})
        if path.path == '/frame':
            name = query.get('name', [''])[0]
            with self.server.lock:
                permitted = len(name) <= 150 and name in self.server.active_names and self.server.is_active()
            if not permitted:
                return self.respond(404, {'error': 'Canale non attivo'})
            try:
                payload = self.server.frame_path(name).read_bytes()
            except OSError:
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        return self.respond(404, {'error': 'Endpoint sconosciuto'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--playlist', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--origin', required=True, help='Exact Jellyfin web origin, e.g. http://host:8096')
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error('workers deve essere tra 1 e 4')
    if not args.origin.startswith(('http://', 'https://')) or urlsplit(args.origin).path not in ('', '/') or urlsplit(args.origin).query or urlsplit(args.origin).fragment:
        parser.error('--origin must be a plain HTTP(S) origin without a path')
    server = PreviewServer((args.host, args.port), Handler, args.playlist,
                           shlex.split(args.ffmpeg), args.cache, args.workers, args.origin)
    print('Anteprime asincrone TV:', args.host, args.port, 'workers:', args.workers, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown_processes()
        server.server_close()


if __name__ == '__main__':
    main()
