#!/usr/bin/env python3
"""Gestore ZAPPING :8100: supervisione delle DUE sezioni separate.

ZAPPING-JELLYFIN :8099 -> sessioni, processi canale, coda, push WebSocket.
ZAPPING-PROVIDERS :8110 -> HLS e decodifica FFmpeg, senza sessioni client.
L'avvio da systemd rimane lo stesso (gestore_iptv.py --playlist ...).
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / 'server_auto.py'
PROVIDER = ROOT / 'zapping_providers' / 'server.py'
LOCK = threading.Lock()


def port_pids(port):
    result = subprocess.run(['fuser', '-n', 'tcp', str(port)], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('fuser: ' + result.stderr.strip())
    return {int(pid) for pid in result.stdout.split() if pid.isdecimal()}


def owned(pid, script):
    try:
        raw = (Path('/proc') / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        return str(script) in raw and 'python' in raw
    except (OSError, ValueError):
        return False


def stop_component(port, script):
    pids = port_pids(port)
    if any(not owned(pid, script) for pid in pids):
        raise RuntimeError('Porta ' + str(port) + ' occupata da un processo sconosciuto')
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not port_pids(port):
            return
        time.sleep(.2)
    for pid in port_pids(port):
        if not owned(pid, script):
            raise RuntimeError('Proprietario porta cambiato: ' + str(port))
        os.kill(pid, signal.SIGKILL)
    for _ in range(30):
        if not port_pids(port):
            return
        time.sleep(.2)
    raise RuntimeError('Porta non liberata: ' + str(port))


def check_status(url, timeout=1):
    with urlopen(url + '/status', timeout=timeout) as response:
        return json.load(response)


def start_component(port, script, command, host, logfile):
    if port_pids(port):
        raise RuntimeError('Porta occupata: ' + str(port))
    with open(ROOT / logfile, 'ab', buffering=0) as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError(str(script) + ' terminato: controllare ' + logfile)
        try:
            check_status('http://' + host + ':' + str(port), timeout=.4)
            return
        except Exception:
            time.sleep(.25)
    process.terminate()
    raise RuntimeError('Servizio non avviato: ' + str(script))


def start_provider(args):
    command = [sys.executable, str(PROVIDER), '--host', args.provider_host,
               '--port', str(args.provider_port), '--ffmpeg', args.ffmpeg,
               '--decode-timeout', str(args.decode_timeout)]
    if args.playlist:
        command.extend(['--playlist', args.playlist])
    start_component(args.provider_port, PROVIDER, command, args.provider_host, 'zapping_providers.log')


def start_zapping(args):
    command = [sys.executable, str(SERVER), '--host', args.host, '--port', str(args.port),
               '--origin', args.origin,
               '--provider-url', 'http://' + args.provider_host + ':' + str(args.provider_port),
               '--response-timeout', str(args.response_timeout), '--sleep', str(args.sleep)]
    start_component(args.port, SERVER, command, args.host, 'server_auto.log')


def backend_request(args, path, data=None, timeout=3):
    address = 'http://' + args.host + ':' + str(args.port) + path
    headers = {'Origin': args.origin}
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode('utf-8')
    with urlopen(Request(address, data=data, headers=headers), timeout=timeout) as response:
        return json.load(response)


def jellyfin_started_at():
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Running}} {{.State.StartedAt}}', 'jellyfin'],
            capture_output=True, text=True, timeout=4)
        if result.returncode != 0:
            return None
        parts = result.stdout.strip().split(maxsplit=1)
        return parts[1] if len(parts) == 2 and parts[0] == 'true' else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def supervisor(args):
    last_jellyfin = jellyfin_started_at()
    while True:
        try:
            with LOCK:
                try:
                    check_status('http://' + args.provider_host + ':' + str(args.provider_port))
                except Exception:
                    stop_component(args.provider_port, PROVIDER)
                    start_provider(args)
                try:
                    backend_request(args, '/status', timeout=2)
                except Exception:
                    stop_component(args.port, SERVER)
                    start_zapping(args)
                started_at = jellyfin_started_at()
                if started_at and last_jellyfin and started_at != last_jellyfin:
                    stop_component(args.port, SERVER)
                    start_zapping(args)
                if started_at:
                    last_jellyfin = started_at
        except Exception as error:
            print('Supervisore ZAPPING:', error, flush=True)
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        return

    def end_headers(self):
        if self.headers.get('Origin') == self.server.origin:
            self.send_header('Access-Control-Allow-Origin', self.server.origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def reply(self, status, data):
        payload = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        if self.headers.get('Origin') != self.server.origin:
            return self.reply(403, {'error': 'Origin non autorizzata'})
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS, GET')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path not in ('/status', '/clients'):
            return self.reply(404, {'error': 'Endpoint sconosciuto'})
        try:
            zapping = backend_request(self.server.config, self.path, timeout=3)
            if self.path == '/clients':
                return self.reply(200, zapping)
            args = self.server.config
            try:
                provider = check_status('http://' + args.provider_host + ':' + str(args.provider_port), timeout=2)
            except Exception:
                provider = None
            return self.reply(200, {
                'service': 'online', 'paused': zapping['paused'],
                'clients': zapping['clients'], 'requested_channels': zapping['channels'],
                'acquiring': zapping['active'], 'network_sessions': zapping['network_sessions'],
                'decoded_frames': zapping['decoded_frames'], 'workers': zapping['workers'],
                'provider_online': provider is not None,
                'provider_decoded_frames': (provider or {}).get('decoded_frames', 0),
                'queue_in': zapping['queue_in'], 'error_frames': zapping['error_frames']})
        except Exception:
            return self.reply(503, {'service': 'zapping_unavailable'})

    def do_POST(self):
        if self.headers.get('Origin') != self.server.origin:
            return self.reply(403, {'error': 'Origin non autorizzata'})
        if self.path not in ('/admin/start', '/admin/stop', '/admin/restart', '/admin/clear-sessions'):
            return self.reply(404, {'error': 'Comando sconosciuto'})
        if not LOCK.acquire(blocking=False):
            return self.reply(409, {'error': 'Operazione in corso'})
        try:
            args = self.server.config
            if self.path == '/admin/restart':
                stop_component(args.port, SERVER)
                stop_component(args.provider_port, PROVIDER)
                start_provider(args)
                start_zapping(args)
                return self.reply(200, {'ok': True, 'action': 'restart'})
            result = backend_request(args, self.path, {}, timeout=10)
            return self.reply(200, result)
        except Exception as error:
            return self.reply(500, {'error': str(error)})
        finally:
            LOCK.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--manager-port', type=int, default=8100)
    parser.add_argument('--provider-host', default='127.0.0.1')
    parser.add_argument('--provider-port', type=int, default=8110)
    parser.add_argument('--origin', default='http://127.0.0.1:8096')
    parser.add_argument('--playlist', default=None)
    parser.add_argument('--cache', type=Path, default=ROOT / 'cache')
    parser.add_argument('--workers', type=int, default=46)
    parser.add_argument('--ffmpeg', default='docker exec -i jellyfin /usr/lib/jellyfin-ffmpeg/ffmpeg')
    parser.add_argument('--decode-timeout', type=float, default=1.0)
    parser.add_argument('--response-timeout', type=float, default=10.0)
    parser.add_argument('--sleep', type=float, default=3.0)
    args = parser.parse_args()
    if len({args.port, args.manager_port, args.provider_port}) != 3:
        parser.error('Le tre porte devono essere distinte')
    if args.workers != 46:
        print('ZAPPING: --workers ignorato; un processo per ogni canale attivo', flush=True)
    server = ThreadingHTTPServer((args.host, args.manager_port), Handler)
    server.config, server.origin = args, args.origin
    if not port_pids(args.provider_port):
        start_provider(args)
    elif any(not owned(pid, PROVIDER) for pid in port_pids(args.provider_port)):
        parser.error('Porta providers occupata da processo sconosciuto')
    if not port_pids(args.port):
        start_zapping(args)
    elif any(not owned(pid, SERVER) for pid in port_pids(args.port)):
        parser.error('Porta ZAPPING occupata da processo sconosciuto')
    print('GESTORE ZAPPING :', args.manager_port, 'JELLYFIN :', args.port,
          'PROVIDERS :', args.provider_port, flush=True)
    threading.Thread(target=supervisor, args=(args,), daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
