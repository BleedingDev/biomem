"""
Session cache for temporary storage of user queries.

Flow:
1. The web client sends RETRIEVE with query + session_id
2. biomem stores the query in the cache under session_id
3. The web client sends STORE with summary + session_id
4. biomem finds the query in the cache under session_id and pairs it with the summary
5. biomem stores into memory: key=query, value=summary
6. The cache entry is deleted

TTL: 10 minutes (configurable). Expired entries are cleaned up automatically.
"""
import threading
import time
from typing import Any, Dict, Optional


class SessionEntry(object):
    """One entry in the session cache."""

    __slots__ = ('user_query', 'created_at', 'metadata')

    def __init__(self, user_query: str, metadata: Optional[Dict[str, Any]] = None):
        self.user_query = user_query
        self.created_at = time.time()
        self.metadata = metadata


class SessionCache(object):
    """
    Cache for pairing a user query (from retrieve) with an LLM summary (from store).

    Thread-safe implementation with automatic cleanup of expired entries.
    """

    def __init__(self, ttl_seconds: int = 600):
        """
        Args:
            ttl_seconds: Entry validity period in seconds (default: 600 = 10 min)
        """
        self._entries = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds

    @property
    def ttl(self):
        return self._ttl_seconds

    def store(self, session_id: str, user_query: str,
              metadata: Optional[Dict[str, Any]] = None) -> None:
        """Stores the user query in the cache under session_id.

        Called when processing the RETRIEVE command.

        Args:
            session_id: Unique session identifier
            user_query: User query
            metadata: Optional metadata (emotion, top_k, ...)
        """
        with self._lock:
            self._entries[session_id] = SessionEntry(user_query, metadata)

    def retrieve(self, session_id: str) -> Optional[str]:
        """Returns the stored user query for the given session_id.

        Called when processing the STORE command for pairing.
        Does not remove the entry (consume() does that).

        Args:
            session_id: Session identifier

        Returns:
            User query string or None if the session does not exist/has expired.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None or self._is_expired(entry):
                return None
            return entry.user_query

    def retrieve_with_metadata(self, session_id: str) -> Optional[SessionEntry]:
        """Returns the full SessionEntry including metadata.

        Args:
            session_id: Session identifier

        Returns:
            SessionEntry or None if the session does not exist/has expired.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None or self._is_expired(entry):
                return None
            return entry

    def consume(self, session_id: str) -> Optional[str]:
        """Returns the user query and removes the entry from the cache.

        Called after successful pairing (store).

        Args:
            session_id: Session identifier

        Returns:
            User query string or None.
        """
        with self._lock:
            entry = self._entries.pop(session_id, None)
            if entry is None or self._is_expired(entry):
                return None
            return entry.user_query

    def has_session(self, session_id: str) -> bool:
        """Checks whether the session exists and is not expired."""
        with self._lock:
            entry = self._entries.get(session_id)
            return entry is not None and not self._is_expired(entry)

    def get_active_count(self) -> int:
        """Returns the number of active (non-expired) entries."""
        with self._lock:
            now = time.time()
            return sum(
                1 for entry in self._entries.values()
                if now - entry.created_at <= self._ttl_seconds
            )

    def cleanup_expired(self) -> int:
        """Removes all expired entries.

        Returns:
            Number of removed entries.
        """
        with self._lock:
            now = time.time()
            expired_ids = [
                sid for sid, entry in self._entries.items()
                if now - entry.created_at > self._ttl_seconds
            ]
            removed = 0
            for sid in expired_ids:
                self._entries.pop(sid, None)
                removed += 1
            return removed

    def remove(self, session_id: str) -> None:
        """Removes an entry from the cache."""
        with self._lock:
            self._entries.pop(session_id, None)

    def _is_expired(self, entry: SessionEntry) -> bool:
        return time.time() - entry.created_at > self._ttl_seconds
