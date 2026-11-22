#!/usr/bin/env python3
"""ZAPPING-JELLYFIN: ONLY sessioni, processi canale, coda e push WebSocket.

NON legge playlist IPTV e NON utilizza FFmpeg: ogni processo canale chiede
un JPEG al server separato ZAPPING-PROVIDERS, su 127.0.0.1:8110.
"""
import argparse
import asyncio
import json
import struct
import sys
from pathlib import Path

from aiohttp import web, WSMsgType


ROOT = Path(__file__).resolve().parent
WORKER = ROOT / 'channel_worker.py'
ERROR_FRAMES = ROOT / 'error_frames'


class ZappingServer:
    def __init__(self, provider_url, allowed_origin, response_timeout, channel_sleep):
        self.provider_url = provider_url
        self.allowed_origin = allowed_origin
        self.response_timeout = response_timeout
        self.channel_sleep = channel_sleep
        self.clients = {}
        self.active_names = set()
        self.channel_tasks = {}
        self.channel_processes = {}
        self.frame_queue = asyncio.Queue(maxsize=128)
        self.latest_frames = {}
        self.errors = {}
        self.decoded_count = 0
        self.distributed_count = 0
        self.error_frames_count = 0
        self.wakeup = asyncio.Event()
        self.manager_task = None
        self.distributor_task = None
        self.paused = False

    async def start(self, app):
        self.manager_task = asyncio.create_task(self.session_manager())
        self.distributor_task = asyncio.create_task(self.distribute_frames())

    async def stop(self, app):
        for websocket in list(self.clients):
            await websocket.close()
        tasks = [task for task in (self.manager_task, self.distributor_task) if task]
        tasks.extend(self.channel_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for process in list(self.channel_processes.values()):
            await self.terminate_worker(process)

    def update_channels(self):
        names = set() if self.paused else {name for client in self.clients.values() for name in client['names']}
        if names != self.active_names:
            removed = self.active_names - names
            self.active_names = names
            for name in removed:
                self.latest_frames.pop(name, None)
                self.errors.pop(name, None)
            self.wakeup.set()

    async def session_manager(self):
        while True:
            names = set(self.active_names)
            for name, task in list(self.channel_tasks.items()):
                if name not in names or task.done():
                    self.channel_tasks.pop(name, None)
                    if not task.done():
                        task.cancel()
            for name in names - self.channel_tasks.keys():
                task = asyncio.create_task(self.channel_loop(name), name='zapping:' + name)
                self.channel_tasks[name] = task
                task.add_done_callback(lambda _: self.wakeup.set())
            self.wakeup.clear()
            await self.wakeup.wait()

    @staticmethod
    async def terminate_worker(process):
        if process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.5)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def channel_loop(self, name):
        """Il task del canale controlla UN processo OS; il processo richiede/itera i frame."""
        process = None
        try:
            command = [sys.executable, str(WORKER), '--name', name,
                       '--provider-url', self.provider_url,
                       '--response-timeout', str(self.response_timeout),
                       '--sleep', str(self.channel_sleep)]
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.channel_processes[name] = process
            while name in self.active_names:
                header = await process.stdout.readexactly(5)
                status = header[:1].decode('ascii')
                length = struct.unpack('!I', header[1:])[0]
                if status not in ('F', 'T', 'D', 'E') or not 0 < length <= 2000000:
                    raise RuntimeError('Protocollo IPC frame non valido')
                jpeg = await process.stdout.readexactly(length)
                if name not in self.active_names:
                    break
                # Coda di INGRESSO separata dalla distribuzione, mai aspettare la trasmissione.
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                        self.frame_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                self.frame_queue.put_nowait((name, status, jpeg))
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, RuntimeError, OSError, ValueError) as exception:
            if name in self.active_names:
                self.errors[name] = 'worker_error: ' + str(exception)[:180]
                jpeg = (ERROR_FRAMES / 'provider_error.jpg').read_bytes()
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                        self.frame_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                self.frame_queue.put_nowait((name, 'E', jpeg))
        finally:
            if process:
                await self.terminate_worker(process)
                if self.channel_processes.get(name) is process:
                    self.channel_processes.pop(name, None)

    async def distribute_frames(self):
        """UN gestore legge la coda comune e smista, senza mai attendere un client."""
        while True:
            name, status, jpeg = await self.frame_queue.get()
            try:
                if name not in self.active_names:
                    continue
                self.latest_frames[name] = jpeg
                if status == 'F':
                    self.decoded_count += 1
                    self.errors.pop(name, None)
                else:
                    self.error_frames_count += 1
                    self.errors[name] = {'T': 'provider_timeout',
                                         'D': 'decode_error',
                                         'E': 'provider_error'}.get(status, 'provider_error')
                for client in self.clients.values():
                    if name in client['names']:
                        # Ultimo frame per canale/client; un client lento non blocca gli altri.
                        client['pending'][name] = jpeg
                        client['event'].set()
                        self.distributed_count += 1
            finally:
                self.frame_queue.task_done()

    async def send_frames(self, websocket, client):
        while not websocket.closed:
            await client['event'].wait()
            client['event'].clear()
            pending = client['pending']
            client['pending'] = {}
            for name, jpeg in pending.items():
                if name in client['names'] and not websocket.closed:
                    await websocket.send_bytes(name.encode('utf-8') + b'\0' + jpeg)

    async def close_sessions(self):
        for websocket in list(self.clients):
            await websocket.close(code=1001, message=b'Reset ZAPPING')
        self.clients.clear()
        self.update_channels()
        self.latest_frames.clear()
        self.errors.clear()


def json_reply(data, status=200):
    return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})


@web.middleware
async def cors(request, handler):
    zapping = request.app['zapping']
    origin = request.headers.get('Origin', '')
    if origin and origin != zapping.allowed_origin:
        return json_reply({'error': 'Origin non autorizzata'}, 403)
    if request.method == 'OPTIONS':
        response = web.Response(status=204)
    else:
        response = await handler(request)
    if origin == zapping.allowed_origin:
        response.headers['Access-Control-Allow-Origin'] = zapping.allowed_origin
        response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


async def websocket_handler(request):
    zapping = request.app['zapping']
    if request.headers.get('Origin') != zapping.allowed_origin:
        return json_reply({'error': 'Origin WebSocket non autorizzata'}, 403)
    websocket = web.WebSocketResponse(heartbeat=20, max_msg_size=16000)
    await websocket.prepare(request)
    client = {'names': set(), 'client_id': '', 'pending': {}, 'event': asyncio.Event()}
    zapping.clients[websocket] = client
    sender = asyncio.create_task(zapping.send_frames(websocket, client))
    try:
        async for message in websocket:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except (ValueError, TypeError):
                await websocket.send_json({'error': 'Messaggio JSON non valido'})
                continue
            if not isinstance(payload, dict) or payload.get('type') != 'subscribe':
                continue
            names = payload.get('names')
            client_id = payload.get('client_id', '')
            if (not isinstance(names, list) or len(names) > 250
                    or any(not isinstance(name, str) or len(name) > 150 for name in names)
                    or not isinstance(client_id, str) or len(client_id) > 128):
                await websocket.send_json({'error': 'Lista Preferiti non valida'})
                continue
            client['client_id'] = client_id
            client['names'] = set(names)
            client['pending'] = {name: zapping.latest_frames[name]
                                 for name in client['names'] if name in zapping.latest_frames}
            if client['pending']:
                client['event'].set()
            zapping.update_channels()
            await websocket.send_json({'type': 'subscribed', 'channels': len(client['names'])})
    finally:
        zapping.clients.pop(websocket, None)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        zapping.update_channels()
        if not zapping.clients:
            zapping.latest_frames.clear()
    return websocket


async def status_handler(request):
    zapping = request.app['zapping']
    return json_reply({'service': 'online', 'type': 'zapping-jellyfin',
                       'active': bool(zapping.active_names),
                       'channels': len(zapping.active_names),
                       'clients': len(zapping.clients),
                       'workers': len(zapping.channel_processes),
                       'network_sessions': len(zapping.channel_processes),
                       'processes': {name: process.pid for name, process in zapping.channel_processes.items()},
                       'queue_in': zapping.frame_queue.qsize(),
                       'pending': sum(len(client['pending']) for client in zapping.clients.values()),
                       'decoded_frames': zapping.decoded_count,
                       'distributed_frames': zapping.distributed_count,
                       'error_frames': zapping.error_frames_count,
                       'errors': dict(zapping.errors), 'paused': zapping.paused})


async def clients_handler(request):
    zapping = request.app['zapping']
    return json_reply({'clients': len(zapping.clients),
                       'channels': len(zapping.active_names),
                       'client_details': [{'client_id': client['client_id'], 'channels': len(client['names'])}
                                          for client in zapping.clients.values()]})


async def state_handler(request):
    zapping = request.app['zapping']
    return json_reply({'frames': {name: True for name in zapping.latest_frames}})


async def frame_handler(request):
    zapping = request.app['zapping']
    name = request.query.get('name', '')
    jpeg = zapping.latest_frames.get(name) if name in zapping.active_names else None
    if jpeg is None:
        return web.Response(status=204)
    return web.Response(body=jpeg, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})


async def channels_handler(request):
    zapping = request.app['zapping']
    return json_reply({'accepted': len(zapping.active_names), 'mode': 'websocket'})


async def focus_handler(request):
    return json_reply({'focused': '', 'mode': 'automatic'})


async def clear_handler(request):
    zapping = request.app['zapping']
    await zapping.close_sessions()
    return json_reply({'ok': True, 'action': 'clear-sessions'})


async def pause_handler(request):
    zapping = request.app['zapping']
    zapping.paused = True
    zapping.update_channels()
    return json_reply({'ok': True, 'action': 'stop'})


async def resume_handler(request):
    zapping = request.app['zapping']
    zapping.paused = False
    zapping.update_channels()
    return json_reply({'ok': True, 'action': 'start'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--provider-url', default='http://127.0.0.1:8110')
    parser.add_argument('--response-timeout', type=float, default=10.0)
    parser.add_argument('--sleep', type=float, default=3.0)
    parser.add_argument('--origin', default='http://127.0.0.1:8096')
    parser.add_argument('--workers', type=int, default=46)
    parser.add_argument('--cache', default=None)
    parser.add_argument('--playlist', default=None)  # Compatibilita' CLI; SOLO providers legge playlist.
    parser.add_argument('--ffmpeg', default=None)   # Compatibilita' CLI; SOLO providers usa FFmpeg.
    args = parser.parse_args()
    if not 0 < args.response_timeout <= 120 or args.sleep < 0:
        parser.error('Timeout risposta / sleep non valido')
    zapping = ZappingServer(args.provider_url, args.origin, args.response_timeout, args.sleep)
    app = web.Application(middlewares=[cors])
    app['zapping'] = zapping
    app.on_startup.append(zapping.start)
    app.on_cleanup.append(zapping.stop)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/status', status_handler)
    app.router.add_get('/clients', clients_handler)
    app.router.add_get('/state', state_handler)
    app.router.add_get('/frame', frame_handler)
    app.router.add_post('/channels', channels_handler)
    app.router.add_post('/focus', focus_handler)
    app.router.add_post('/admin/clear-sessions', clear_handler)
    app.router.add_post('/admin/stop', pause_handler)
    app.router.add_post('/admin/start', resume_handler)
    app.router.add_route('OPTIONS', '/{tail:.*}', channels_handler)
    print('ZAPPING-JELLYFIN', args.host, args.port, 'provider:', args.provider_url, flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None, handle_signals=True)


if __name__ == '__main__':
    main()
