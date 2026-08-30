'''
Thread Store — encrypted SQLite storage for conversation threads.
Each thread's content is AES-256-GCM encrypted with the same key as SettingsManager.
'''
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger('bdbm.thread_store')

_NONCE_SIZE = 12

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except ImportError:
    _HAS_AESGCM = False


class ThreadStore:
    '''
    Encrypted SQLite storage for conversation threads.
    Re-uses the AES-256-GCM key from SettingsManager (HW fingerprint derived).

    Schema:
        threads(id TEXT PK, title TEXT, timestamp INTEGER, nonce BLOB, ciphertext BLOB)
    '''

    def __init__(self, data_dir: Path, aes_key: bytes, hmac_key: bytes):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(self._dir / 'threads.db')
        self._aes_key = aes_key
        self._hmac_key = hmac_key
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL DEFAULT 'New chat',
                    timestamp   INTEGER NOT NULL,
                    nonce       BLOB,
                    ciphertext  BLOB
                )
            """
            )
            conn.commit()

    def _encrypt(self, data: str):
        raw = data.encode('utf-8')
        if _HAS_AESGCM:
            nonce = os.urandom(_NONCE_SIZE)
            ct = AESGCM(self._aes_key).encrypt(nonce, raw, None)
            return nonce, ct
        key_ext = self._aes_key * (len(raw) // len(self._aes_key) + 1)
        obf = bytes(a ^ b for a, b in zip(raw, key_ext))
        return b'', obf

    def _decrypt(self, nonce: bytes, ciphertext: bytes) -> str:
        if _HAS_AESGCM and nonce:
            raw = AESGCM(self._aes_key).decrypt(bytes(nonce), bytes(ciphertext), None)
        else:
            ct = bytes(ciphertext)
            key_ext = self._aes_key * (len(ct) // len(self._aes_key) + 1)
            raw = bytes(a ^ b for a, b in zip(ct, key_ext))
        return raw.decode('utf-8')

    def save_thread(self, thread_id: str, title: str, timestamp: int, history: List[Dict]):
        '''Encrypt and persist a thread.'''
        json_str = json.dumps(history, ensure_ascii=False)
        nonce, ct = self._encrypt(json_str)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO threads (id, title, timestamp, nonce, ciphertext) VALUES (?,?,?,?,?)',
                (thread_id, title or 'New chat', timestamp, nonce, ct),
            )
            conn.commit()

    def load_thread(self, thread_id: str) -> List[Dict]:
        '''Load and decrypt a thread's history.'''
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                'SELECT nonce, ciphertext FROM threads WHERE id = ?', (thread_id,)
            ).fetchone()
        if not row:
            return []
        try:
            data = self._decrypt(row[0], row[1])
            result = json.loads(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f'ThreadStore: load_thread({thread_id}) failed: {e}')
            return []

    def get_thread_list(self) -> List[Dict]:
        '''Return [{id, title, timestamp}] sorted newest first.'''
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                'SELECT id, title, timestamp FROM threads ORDER BY timestamp DESC'
            ).fetchall()
        return [{'id': r[0], 'title': r[1], 'timestamp': r[2]} for r in rows]

    def delete_thread(self, thread_id: str):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('DELETE FROM threads WHERE id = ?', (thread_id,))
            conn.commit()

    def rename_thread(self, thread_id: str, new_title: str):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('UPDATE threads SET title = ? WHERE id = ?', (new_title, thread_id))
            conn.commit()

    def update_title_if_new(self, thread_id: str, new_title: str, timestamp: int):
        '''Set title only if current title is still 'New chat'.'''
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                'SELECT title FROM threads WHERE id = ?', (thread_id,)
            ).fetchone()
            if row and row[0] in ('New chat', '') and new_title:
                conn.execute(
                    'UPDATE threads SET title = ?, timestamp = ? WHERE id = ?',
                    (new_title, timestamp, thread_id),
                )
                conn.commit()
