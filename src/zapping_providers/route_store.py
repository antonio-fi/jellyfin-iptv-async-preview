"""Preferenze persistenti delle sorgenti ZAPPING-PROVIDERS.

Le preferenze sono associate alla firma delle sorgenti originali del canale:
quando cambia la playlist, un vecchio URL non viene riutilizzato per errore.
Non contiene utenti Jellyfin, sessioni, password o fotogrammi.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class RouteStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        is_new = not self.db_path.exists()
        self.connection = sqlite3.connect(str(self.db_path), timeout=3)
        if is_new:
            os.chmod(self.db_path, 0o600)
        self.connection.execute('''
            CREATE TABLE IF NOT EXISTS preferred_sources (
                channel TEXT NOT NULL,
                playlist_digest TEXT NOT NULL,
                source_url TEXT NOT NULL,
                agent TEXT NOT NULL,
                source_label TEXT NOT NULL,
                resolved_url TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                PRIMARY KEY (channel, playlist_digest)
            )
        ''')
        # Compatibile con un eventuale database creato dalla versione precedente.
        fields = {row[1] for row in self.connection.execute('PRAGMA table_info(preferred_sources)')}
        if 'resolved_url' not in fields:
            self.connection.execute("ALTER TABLE preferred_sources ADD COLUMN resolved_url TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

    def get(self, channel, playlist_digest):
        row = self.connection.execute(
            '''SELECT source_url, agent, source_label, resolved_url FROM preferred_sources
               WHERE channel=? AND playlist_digest=?''',
            (channel, playlist_digest)).fetchone()
        return tuple(row) if row else None

    def save(self, channel, playlist_digest, source_url, agent, source_label, resolved_url):
        current = self.get(channel, playlist_digest)
        if current == (source_url, agent, source_label, resolved_url):
            return False  # Evita una scrittura SQLite per ogni fotogramma.
        now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.connection.execute('''
            INSERT INTO preferred_sources
                (channel, playlist_digest, source_url, agent, source_label, resolved_url, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, playlist_digest) DO UPDATE SET
                source_url=excluded.source_url,
                agent=excluded.agent,
                source_label=excluded.source_label,
                resolved_url=excluded.resolved_url,
                confirmed_at=excluded.confirmed_at
        ''', (channel, playlist_digest, source_url, agent, source_label, resolved_url, now_utc))
        self.connection.commit()
        return True

    def count(self):
        return self.connection.execute('SELECT COUNT(*) FROM preferred_sources').fetchone()[0]

    def close(self):
        self.connection.close()
