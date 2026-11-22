#!/usr/bin/env python3
"""Compatibilita' servizio esistente: avvia SOLO ZAPPING-JELLYFIN (porta 8099)."""
from zapping_jellyfin.server import main


if __name__ == '__main__':
    main()
