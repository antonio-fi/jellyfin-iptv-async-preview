#!/usr/bin/env python3
"""Test locale: HLS AES-128, IV implicito/esplicito, redirect, NONE e SAMPLE-AES."""
import asyncio
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from zapping_providers.server import ProviderServer, ProviderError
from zapping_providers.route_store import RouteStore


def encrypt_segment(data, key, iv):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()
    except ImportError:
        process = subprocess.run(
            ['openssl', 'enc', '-e', '-aes-128-cbc', '-K', key.hex(), '-iv', iv.hex()],
            input=data, capture_output=True, check=True)
        return process.stdout


async def test():
    key = bytes(range(16))
    key2 = bytes(range(16, 32))
    iv = bytes.fromhex('00' * 15 + '2a')
    data = b'\x47' * 188 + b'\x47' * 188
    data2 = b'\x47' * 188 * 3
    encrypted_41 = encrypt_segment(data, key, (41).to_bytes(16, 'big'))
    encrypted_42 = encrypt_segment(data2, key, iv)
    requested_paths = []

    async def handle(request):
        requested_paths.append(request.path)
        if request.path == '/relinker/start':
            raise web.HTTPFound('/cdn/master.m3u8')
        if request.path == '/cdn/master.m3u8':
            return web.Response(text='''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=426x240
video/index.m3u8
''')
        if request.path == '/cdn/video/index.m3u8':
            return web.Response(text='''#EXTM3U
#EXT-X-MEDIA-SEQUENCE:41
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:4,
segment41.ts
#EXT-X-KEY:METHOD=AES-128,URI="key2.bin",IV=0x0000000000000000000000000000002A
#EXTINF:4,
segment42.ts
''')
        if request.path == '/cdn/video/key.bin':
            return web.Response(body=key)
        if request.path == '/cdn/video/key2.bin':
            return web.Response(body=key)
        if request.path == '/cdn/video/segment41.ts':
            return web.Response(body=encrypted_41)
        if request.path == '/cdn/video/segment42.ts':
            return web.Response(body=encrypted_42)
        if request.path == '/plain/index.m3u8':
            return web.Response(text='''#EXTM3U
#EXT-X-KEY:METHOD=NONE
#EXTINF:3,
segment.ts
''')
        if request.path == '/plain/segment.ts':
            return web.Response(body=b'UNENCRYPTED SEGMENT')
        if request.path == '/unsupported/index.m3u8':
            return web.Response(text='''#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"
#EXTINF:3,
segment.ts
''')
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
                        'Rai simulato', base_url + '/relinker/start', 'Mozilla/5.0')
                    assert segment == data2, (len(segment), len(data2))
                    assert segment_url.endswith('/segment42.ts'), segment_url
                    assert effective_url.endswith('/cdn/video/index.m3u8'), effective_url
                    assert '/cdn/video/key2.bin' in requested_paths
                    print('OK: AES-128, IV esplicito, redirect CDN, chiave relativa')

                    iv_segment_url, iv_segment, _ = await provider.acquire_segment(
                        'Rai simulato 41', base_url + '/cdn/video/index.m3u8', 'Mozilla/5.0')
                    assert iv_segment == data2
                    original = await provider.decrypt_hls(
                        encrypted_41, (base_url + '/cdn/video/key.bin', None), 41, 'Mozilla/5.0')
                    assert original == data
                    print('OK: IV implicito da MEDIA-SEQUENCE')

                    _, plain, _ = await provider.acquire_segment(
                        'Senza cifratura', base_url + '/plain/index.m3u8', 'Mozilla/5.0')
                    assert plain == b'UNENCRYPTED SEGMENT'
                    print('OK: METHOD=NONE non viene scambiato per cifratura')

                    try:
                        await provider.acquire_segment(
                            'Protezione non supportata', base_url + '/unsupported/index.m3u8',
                            'Mozilla/5.0')
                    except ProviderError as exception:
                        assert 'SAMPLE-AES' in str(exception), str(exception)
                        print('OK: SAMPLE-AES identificato senza tentare decrittazione')
                    else:
                        raise AssertionError('SAMPLE-AES non segnalato')
                finally:
                    await provider.stop(None)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(test())
