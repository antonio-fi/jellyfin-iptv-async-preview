#!/usr/bin/env python3
"""Test di regressione offline con HTTP locale e redirect HLS."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from zapping_providers.server import ProviderServer
from zapping_providers.route_store import RouteStore


async def test():
    requested_paths = []

    async def handle(request):
        requested_paths.append(request.path)
        if request.path == '/relinker/relinkerServlet.htm':
            raise web.HTTPFound('/cdn/master.m3u8')
        if request.path == '/cdn/master.m3u8':
            return web.Response(text='''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=180000,RESOLUTION=426x240
video/index.m3u8
''', content_type='application/vnd.apple.mpegurl')
        if request.path == '/cdn/video/index.m3u8':
            return web.Response(text='''#EXTM3U
#EXTINF:3.0,
segment.ts
''', content_type='application/vnd.apple.mpegurl')
        if request.path == '/cdn/video/segment.ts':
            return web.Response(body=b'VIDEO SEGMENT')
        if request.path == '/plain/index.m3u8':
            return web.Response(text='''#EXTM3U
#EXTINF:3.0,
segment.ts
''', content_type='application/vnd.apple.mpegurl')
        if request.path == '/plain/segment.ts':
            return web.Response(body=b'PLAIN SEGMENT')
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_get('/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = 'http://127.0.0.1:' + str(port)

    try:
        with tempfile.TemporaryDirectory() as folder:
            with patch('zapping_providers.server.RouteStore',
                       lambda path: RouteStore(Path(folder) / 'test.sqlite3')):
                provider = ProviderServer(None, ['true'], 1.0)
                await provider.start(None)
                try:
                    segment_url, segment, effective_url = await provider.acquire_segment(
                        'Canale di test',
                        base_url + '/relinker/relinkerServlet.htm?cont=test', 'Mozilla/5.0')
                    assert segment_url == base_url + '/cdn/video/segment.ts', segment_url
                    assert segment == b'VIDEO SEGMENT', segment
                    assert effective_url == base_url + '/cdn/video/index.m3u8', effective_url
                    assert '/relinker/video/index.m3u8' not in requested_paths, requested_paths
                    print('OK: redirect 302, master HLS, variante e segmento relativi su CDN')

                    segment_url, segment, effective_url = await provider.acquire_segment(
                        'Canale semplice', base_url + '/plain/index.m3u8', 'Mozilla/5.0')
                    assert segment_url == base_url + '/plain/segment.ts', segment_url
                    assert segment == b'PLAIN SEGMENT', segment
                    assert effective_url == base_url + '/plain/index.m3u8', effective_url
                    print('OK: sorgente HLS normale senza redirect')
                finally:
                    await provider.stop(None)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(test())
