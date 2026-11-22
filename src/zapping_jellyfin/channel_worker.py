#!/usr/bin/env python3
"""UN processo OS per canale attivo: richiesta HTTP al provider -> JPEG -> stdout.

Protocollo IPC: un byte tipo (F/T/D/E) + uint32 big-endian lunghezza + JPEG.
Il processo NON conosce utenti, WebSocket o altre sorgenti.
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


ERROR_FRAMES = Path(__file__).resolve().parent / 'error_frames'


def request_frame(provider_url, name, timeout):
    address = provider_url.rstrip('/') + '/frame?' + urlencode({'name': name})
    try:
        with urlopen(address, timeout=timeout) as response:
            if response.status == 204:
                return None, None
            if response.status != 200 or response.headers.get_content_type() != 'image/jpeg':
                return 'E', None
            data = response.read(2000001)
            if len(data) > 2000000 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
                return 'E', None
            return 'F', data
    except HTTPError as exception:
        try:
            data = json.loads(exception.read(2048).decode('utf-8', 'replace'))
            error = data.get('error')
        except (ValueError, AttributeError):
            error = None
        if error == 'decode_error':
            return 'D', None
        if error == 'provider_timeout':
            return 'T', None
        return 'E', None
    except (TimeoutError, URLError) as exception:
        # Solo un timeout e' PROVIDER TIMEOUT. Connessione rifiutata -> PROVIDER ERROR.
        reason = getattr(exception, 'reason', exception)
        if isinstance(exception, TimeoutError) or isinstance(reason, TimeoutError):
            return 'T', None
        return 'E', None
    except (OSError, ValueError):
        return 'E', None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--provider-url', required=True)
    parser.add_argument('--response-timeout', type=float, default=10.0)
    parser.add_argument('--sleep', type=float, default=3.0)
    args = parser.parse_args()
    if not 0 < args.response_timeout <= 120 or args.sleep < 0:
        parser.error('Timeout o sleep non valido')
    images = {'T': (ERROR_FRAMES / 'provider_timeout.jpg').read_bytes(),
              'D': (ERROR_FRAMES / 'decode_error.jpg').read_bytes(),
              'E': (ERROR_FRAMES / 'provider_error.jpg').read_bytes()}
    while True:
        status, data = request_frame(args.provider_url, args.name, args.response_timeout)
        if status is not None:
            frame = data if status == 'F' else images[status]
            try:
                sys.stdout.buffer.write(status.encode('ascii') + struct.pack('!I', len(frame)) + frame)
                sys.stdout.buffer.flush()
            except BrokenPipeError:
                return
        # Pausa PER-CANALE: nessun cron/timer centrale, nessuna decodifica concorrente dello stesso canale.
        time.sleep(args.sleep)


if __name__ == '__main__':
    main()
