"""Test locali simulati: non servono canali IPTV online."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientResponseError
from yarl import URL

from zapping_providers.server import ProviderServer
from zapping_providers.route_store import RouteStore


def http_404(url):
    request_info = type('RequestInfo', (), {'real_url': URL(url)})()
    return ClientResponseError(request_info, (), status=404, message='Not Found')


async def run():
    with tempfile.TemporaryDirectory() as folder:
        playlist = Path(folder, 'channels.m3u8')
        playlist.write_text('''#EXTM3U
#EXTINF:-1,Canale qualsiasi
https://example.org/one.m3u8
#EXTINF:-1,Canale qualsiasi
https://example.org/two.m3u8
#EXTINF:-1,Nome diverso dalla Rai
https://mediapolis.rai.it/relinker/relinkerServlet.htm?cont=746966&output=7
''')
        store_path = Path(folder, 'sources.sqlite3')
        with patch('zapping_providers.server.RouteStore', lambda path: RouteStore(store_path)):
            provider = ProviderServer(playlist, ['true'], 1.0)
            calls = []
            async def mock_hls(url, agent, name=None):
                calls.append(url)
                if 'one.m3u8' in url or ('/relinker/' in url and 'output=16' not in url):
                    raise http_404(url)
                return (url.replace('.m3u8', '.ts'), None, 0), None, url
            async def mock_fetch(url, agent, binary=False):
                return b'SEGMENT'
            async def mock_decode(name, segment):
                return b'\xff\xd8IMAGE\xff\xd9'
            provider.hls_segment = mock_hls
            provider.fetch_data = mock_fetch
            provider.decode = mock_decode
            assert await provider.get_frame('Canale qualsiasi') == b'\xff\xd8IMAGE\xff\xd9'
            assert calls == ['https://example.org/one.m3u8', 'https://example.org/two.m3u8'], calls
            assert provider.source_routes['Canale qualsiasi'] == 'playlist #2'
            assert provider.route_store.count() == 1
            assert await provider.get_frame('Nome diverso dalla Rai') == b'\xff\xd8IMAGE\xff\xd9'
            assert provider.rai_routes['Nome diverso dalla Rai'] == 'relinker output=16'
            assert provider.route_store.count() == 2
            provider.route_store.close()

            # Simula il riavvio: parte immediatamente dalla sorgente funzionante.
            provider2 = ProviderServer(playlist, ['true'], 1.0)
            calls.clear()
            provider2.hls_segment = mock_hls
            provider2.fetch_data = mock_fetch
            provider2.decode = mock_decode
            assert await provider2.get_frame('Canale qualsiasi') == b'\xff\xd8IMAGE\xff\xd9'
            assert calls == ['https://example.org/two.m3u8'], calls

            # Il collegamento salvato smette di funzionare: fallisce e torna a cercare.
            # La prima sorgente viene ripristinata, la seconda fallisce.
            calls.clear()
            async def switched_hls(url, agent, name=None):
                calls.append(url)
                if 'two.m3u8' in url:
                    raise http_404(url)
                return (url.replace('.m3u8', '.ts'), None, 0), None, url
            provider2.hls_segment = switched_hls
            assert await provider2.get_frame('Canale qualsiasi') == b'\xff\xd8IMAGE\xff\xd9'
            assert calls[:2] == ['https://example.org/two.m3u8', 'https://example.org/one.m3u8'], calls
            assert provider2.source_routes['Canale qualsiasi'] == 'playlist #1'
            provider2.route_store.close()

            # Cambia la playlist -> non usa la vecchia sorgente memorizzata.
            playlist.write_text('''#EXTM3U
#EXTINF:-1,Canale qualsiasi
https://example.org/new.m3u8
''')
            provider3 = ProviderServer(playlist, ['true'], 1.0)
            calls.clear()
            provider3.hls_segment = switched_hls
            provider3.fetch_data = mock_fetch
            provider3.decode = mock_decode
            assert await provider3.get_frame('Canale qualsiasi') == b'\xff\xd8IMAGE\xff\xd9'
            assert calls == ['https://example.org/new.m3u8'], calls
            provider3.route_store.close()

        # Variante HLS video numero 1 non trovata; usa la numero 2.
        with patch('zapping_providers.server.RouteStore', lambda path: RouteStore(store_path)):
            provider4 = ProviderServer(playlist, ['true'], 1.0)
            calls.clear()
            async def fake_fetch(url, agent, binary=False, with_final_url=False):
                calls.append(url)
                if url.endswith('/master.m3u8'):
                    content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=640x360
missing.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=1280x720
working.m3u8
"""
                elif url.endswith('/missing.m3u8'):
                    raise http_404(url)
                elif url.endswith('/working.m3u8'):
                    content = '#EXTM3U\n#EXTINF:6.0,\nsegment.ts\n'
                else:
                    content = b'SEGMENT'
                return (content, url) if with_final_url else content
            provider4.fetch_data = fake_fetch
            segment_info, init_info, resolved_url = await provider4.hls_segment('https://example.org/master.m3u8', 'Mozilla/5.0')
            assert segment_info[0] == 'https://example.org/segment.ts'
            assert init_info is None
            assert resolved_url == 'https://example.org/working.m3u8'
            assert calls == ['https://example.org/master.m3u8', 'https://example.org/missing.m3u8',
                             'https://example.org/working.m3u8'], calls
            provider4.route_store.close()

        # Un decoder fallisce sulla prima variante, al giro successivo passa
        # automaticamente alla successiva, senza bloccare altri canali.
        with patch('zapping_providers.server.RouteStore', lambda path: RouteStore(store_path)):
            playlist.write_text("""#EXTM3U
#EXTINF:-1,Canale video
https://example.org/master.m3u8
""")
            provider5 = ProviderServer(playlist, ['true'], 1.0)
            async def media_fetch(url, agent, binary=False, with_final_url=False):
                if url.endswith('master.m3u8'):
                    content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=640x360
bad.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=1280x720
good.m3u8
"""
                elif url.endswith(('bad.m3u8', 'good.m3u8')):
                    content = '#EXTM3U\n#EXTINF:6.0,\n' + ('bad.ts' if 'bad' in url else 'good.ts') + '\n'
                else:
                    content = b'BAD' if url.endswith('bad.ts') else b'GOOD'
                return (content, url) if with_final_url else content
            async def media_decode(name, segment):
                from zapping_providers.server import DecodeError
                if segment == b'BAD':
                    raise DecodeError('Decodifica non riuscita')
                return b'\xff\xd8OK\xff\xd9'
            provider5.fetch_data = media_fetch
            provider5.decode = media_decode
            from zapping_providers.server import DecodeError
            try:
                await provider5.get_frame('Canale video')
                assert False, 'La prima variante deve fallire'
            except DecodeError:
                pass
            assert await provider5.get_frame('Canale video') == b'\xff\xd8OK\xff\xd9'
            assert provider5.route_store.get('Canale video',
                provider5.playlist_digest(provider5.channels['Canale video']))[3] == 'https://example.org/good.m3u8'
            provider5.route_store.close()

            # Riavvio: usa direttamente la variante video persistita,
            # senza interrogare nuovamente il master e la variante difettosa.
            provider6 = ProviderServer(playlist, ['true'], 1.0)
            media_calls = []
            async def watched_fetch(url, agent, binary=False, with_final_url=False):
                media_calls.append(url)
                return await media_fetch(url, agent, binary, with_final_url)
            provider6.fetch_data = watched_fetch
            provider6.decode = media_decode
            assert await provider6.get_frame('Canale video') == b'\xff\xd8OK\xff\xd9'
            assert media_calls[0] == 'https://example.org/good.m3u8', media_calls
            provider6.route_store.close()


if __name__ == '__main__':
    asyncio.run(run())
    print('OK: fallback, SQLite, riavvio, cambio sorgente, varianti HLS 404/decode')
