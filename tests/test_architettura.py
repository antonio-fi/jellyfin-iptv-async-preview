#!/usr/bin/env python3
"""Test locali senza Jellyfin reale, Docker o playlist reale."""
import asyncio
import sys
import tempfile
from pathlib import Path

from aiohttp import web, ClientSession, WSMsgType

from zapping_jellyfin import server as jellyfin
from zapping_providers import server as providers


async def main():
    async def playlist(request):
        return web.Response(text='#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\n' + request.match_info['name'] + '.ts\n')

    async def segment(request):
        name = request.match_info['name']
        if name == 'nove':
            await asyncio.sleep(7)
        return web.Response(body=name.encode())

    source = web.Application()
    source.router.add_get('/{name}.m3u8', playlist)
    source.router.add_get('/{name}.ts', segment)
    runner_source = web.AppRunner(source)
    await runner_source.setup()
    await web.TCPSite(runner_source, host='127.0.0.1', port=19211).start()
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir)
        (path / 'frame.jpg').write_bytes((jellyfin.ERROR_FRAMES / 'provider_error.jpg').read_bytes())
        fake = path / 'ffmpeg_fake.py'
        fake.write_text('''import sys, time
from pathlib import Path
data = sys.stdin.buffer.read()
if data == b'canale5':
    time.sleep(4)
else:
    sys.stdout.buffer.write(Path(__file__).with_name('frame.jpg').read_bytes())
''')
        playlist_file = path / 'playlist.m3u8'
        playlist_file.write_text('#EXTM3U\n'
                                 '#EXTINF:-1,Rai 1\nhttp://127.0.0.1:19211/rai1.m3u8\n'
                                 '#EXTINF:-1,Canale 5\nhttp://127.0.0.1:19211/canale5.m3u8\n'
                                 '#EXTINF:-1,Nove\nhttp://127.0.0.1:19211/nove.m3u8\n')
        provider = providers.ProviderServer(playlist_file, [sys.executable, str(fake)], 1.0)
        app_provider = web.Application()
        app_provider['provider'] = provider
        app_provider.on_startup.append(provider.start)
        app_provider.on_cleanup.append(provider.stop)
        app_provider.router.add_get('/frame', providers.frame_handler)
        app_provider.router.add_get('/status', providers.status_handler)
        runner_provider = web.AppRunner(app_provider)
        await runner_provider.setup()
        await web.TCPSite(runner_provider, '127.0.0.1', 19212).start()

        zapping = jellyfin.ZappingServer('http://127.0.0.1:19212', 'http://127.0.0.1:8096', 2.0, .2)
        app_zapping = web.Application(middlewares=[jellyfin.cors])
        app_zapping['zapping'] = zapping
        app_zapping.on_startup.append(zapping.start)
        app_zapping.on_cleanup.append(zapping.stop)
        app_zapping.router.add_get('/ws', jellyfin.websocket_handler)
        app_zapping.router.add_get('/status', jellyfin.status_handler)
        runner_zapping = web.AppRunner(app_zapping)
        await runner_zapping.setup()
        await web.TCPSite(runner_zapping, '127.0.0.1', 19213).start()

        async with ClientSession() as http:
            ws_a = await http.ws_connect('http://127.0.0.1:19213/ws', headers={'Origin': 'http://127.0.0.1:8096'})
            ws_b = await http.ws_connect('http://127.0.0.1:19213/ws', headers={'Origin': 'http://127.0.0.1:8096'})
            await ws_a.send_json({'type': 'subscribe', 'client_id': 'A', 'names': ['Rai 1', 'Canale 5']})
            await ws_b.send_json({'type': 'subscribe', 'client_id': 'B', 'names': ['Rai 1', 'Nove']})
            async def collect(ws, required):
                received = {}
                while not required.issubset(received):
                    message = await ws.receive(timeout=10)
                    if message.type == WSMsgType.BINARY:
                        name, frame = message.data.split(b'\0', 1)
                        name = name.decode('utf-8')
                        assert frame.startswith(b'\xff\xd8') and frame.endswith(b'\xff\xd9')
                        received[name] = frame
                return received
            results_a, results_b = await asyncio.gather(collect(ws_a, {'Rai 1', 'Canale 5'}),
                                                        collect(ws_b, {'Rai 1', 'Nove'}))
            assert set(results_a) == {'Rai 1', 'Canale 5'}, results_a
            assert set(results_b) == {'Rai 1', 'Nove'}, results_b
            assert results_a['Canale 5'] == (jellyfin.ERROR_FRAMES / 'decode_error.jpg').read_bytes()
            assert results_b['Nove'] == (jellyfin.ERROR_FRAMES / 'provider_timeout.jpg').read_bytes()
            async with http.get('http://127.0.0.1:19213/status') as response:
                status = await response.json()
                print('ZAPPING', {'clients': status['clients'], 'channels': status['channels'],
                                  'workers': status['network_sessions'], 'queue': status['queue_in'],
                                  'frames': status['decoded_frames'], 'errors': status['errors']})
                assert status['clients'] == 2 and status['channels'] == 3
                assert status['network_sessions'] == 3
                assert status['errors']['Canale 5'] == 'decode_error'
                assert status['errors']['Nove'] == 'provider_timeout'
            async with http.get('http://127.0.0.1:19212/status') as response:
                status = await response.json()
                print('PROVIDERS', {'requests': status['requests'], 'decoded': status['decoded_frames'],
                                    'errors': status['errors']})
                assert status['decoded_frames'] >= 1
            await ws_a.close()
            await asyncio.sleep(.3)
            assert zapping.active_names == {'Rai 1', 'Nove'}
            await ws_b.close()
            await asyncio.sleep(.3)
            assert not zapping.active_names
        await runner_zapping.cleanup()
        await runner_provider.cleanup()
    await runner_source.cleanup()
    print('TEST OK: sezioni distinte; 3 processi OS; coda comune; filtro 2 client; timeout provider/decode; arresto processi')


if __name__ == '__main__':
    asyncio.run(main())
