from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

RETENTION_SECONDS = 24 * 60 * 60
SESSION_LEASE_SECONDS = 75
MESSAGE_COLUMNS = """messages.message_id, messages.sender_key, messages.sender_name,
    messages.message_type, messages.text_content, messages.media_id,
    messages.created_at, messages.expires_at, messages.recipient_key,
    messages.recipient_name, messages.recipient_name_key, messages.sender_name_key,
    media.original_name AS original_name, media.file_category AS file_category,
    media.byte_size AS attachment_size"""
MESSAGE_TABLE = """FROM messages
    LEFT JOIN media ON media.media_id = messages.media_id"""


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "ajoechat.sqlite3"
        self.attachment_dir = data_dir / "attachments"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attachment_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = self._connect()
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    joined_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    sender_key TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text_content TEXT,
                    media_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    recipient_key TEXT,
                    recipient_name TEXT,
                    recipient_name_key TEXT,
                    sender_name_key TEXT
                );

                CREATE INDEX IF NOT EXISTS messages_expiry
                    ON messages(expires_at);
                CREATE INDEX IF NOT EXISTS messages_recent
                    ON messages(created_at DESC);
                CREATE TABLE IF NOT EXISTS media (
                    media_id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    original_name TEXT NOT NULL DEFAULT 'Shared file',
                    file_category TEXT NOT NULL DEFAULT 'image',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS media_expiry
                    ON media(expires_at);
                """)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            for column, statement in {
                "recipient_key": "ALTER TABLE messages ADD COLUMN recipient_key TEXT",
                "recipient_name": "ALTER TABLE messages ADD COLUMN recipient_name TEXT",
                "recipient_name_key": "ALTER TABLE messages ADD COLUMN recipient_name_key TEXT",
                "sender_name_key": "ALTER TABLE messages ADD COLUMN sender_name_key TEXT",
            }.items():
                if column not in columns:
                    connection.execute(statement)
            media_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(media)").fetchall()
            }
            for column, statement in {
                "original_name": "ALTER TABLE media ADD COLUMN original_name TEXT NOT NULL DEFAULT 'Shared file'",
                "file_category": "ALTER TABLE media ADD COLUMN file_category TEXT NOT NULL DEFAULT 'image'",
            }.items():
                if column not in media_columns:
                    connection.execute(statement)
            connection.execute("""CREATE INDEX IF NOT EXISTS messages_private_sender
                   ON messages(sender_key, recipient_name_key, created_at DESC)""")
            connection.execute("""CREATE INDEX IF NOT EXISTS messages_private_recipient
                   ON messages(recipient_key, sender_name_key, created_at DESC)""")
            connection.commit()
        finally:
            connection.close()
        self.database_path.chmod(0o600)

    async def join(self, token_hash: str, username: str, username_key: str) -> bool:
        return await asyncio.to_thread(self._join, token_hash, username, username_key)

    def _join(self, token_hash: str, username: str, username_key: str) -> bool:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            try:
                connection.execute(
                    """INSERT INTO sessions
                       (token_hash, username, username_key, joined_at, last_seen, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        token_hash,
                        username,
                        username_key,
                        now,
                        now,
                        now + SESSION_LEASE_SECONDS,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
            return True
        finally:
            connection.close()

    async def session(self, token_hash: str | None) -> dict[str, Any] | None:
        if not token_hash:
            return None
        return await asyncio.to_thread(self._session, token_hash)

    def _session(self, token_hash: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT token_hash, username, username_key, joined_at, expires_at
                   FROM sessions WHERE token_hash = ? AND expires_at > ?""",
                (token_hash, time.time()),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def active_session_for_name(self, username_key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._active_session_for_name, username_key)

    def _active_session_for_name(self, username_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT token_hash, username, username_key, joined_at, expires_at
                   FROM sessions WHERE username_key = ? AND expires_at > ?""",
                (username_key, time.time()),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def refresh_session(self, token_hash: str) -> bool:
        return await asyncio.to_thread(self._refresh_session, token_hash)

    def _refresh_session(self, token_hash: str) -> bool:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute(
                """UPDATE sessions
                   SET last_seen = ?, expires_at = ?
                   WHERE token_hash = ? AND expires_at > ?""",
                (now, now + SESSION_LEASE_SECONDS, token_hash, now),
            )
            connection.commit()
            return connection.total_changes > 0
        finally:
            connection.close()

    async def leave(self, token_hash: str) -> None:
        await asyncio.to_thread(self._leave, token_hash)

    def _leave(self, token_hash: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
            )
            connection.commit()
        finally:
            connection.close()

    async def add_text_message(
        self,
        token_hash: str,
        content: str,
        message_id: str,
        recipient: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._add_text_message, token_hash, content, message_id, recipient
        )

    def _add_text_message(
        self,
        token_hash: str,
        content: str,
        message_id: str,
        recipient: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        now = time.time()
        expires_at = now + RETENTION_SECONDS
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT username, username_key FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
            if not session:
                connection.rollback()
                return None
            target = None
            if recipient:
                target = connection.execute(
                    """SELECT token_hash, username, username_key FROM sessions
                       WHERE token_hash = ? AND username_key = ? AND expires_at > ?""",
                    (recipient["token_hash"], recipient["username_key"], now),
                ).fetchone()
                if not target or target["token_hash"] == token_hash:
                    connection.rollback()
                    return None
            existing = connection.execute(
                """SELECT message_id, sender_name, message_type, text_content,
                          media_id, created_at, expires_at, recipient_key,
                          recipient_name, recipient_name_key, sender_name_key
                   FROM messages WHERE message_id = ? AND sender_key = ?""",
                (message_id, token_hash),
            ).fetchone()
            if existing:
                expected_recipient = target["token_hash"] if target else None
                if (
                    existing["recipient_key"] != expected_recipient
                    or existing["text_content"] != content
                ):
                    connection.rollback()
                    return None
                connection.rollback()
                return self._map_message_row(existing)
            connection.execute(
                """INSERT INTO messages
                   (message_id, sender_key, sender_name, message_type, text_content,
                    media_id, created_at, expires_at, recipient_key, recipient_name,
                    recipient_name_key, sender_name_key)
                   VALUES (?, ?, ?, 'text', ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    token_hash,
                    session["username"],
                    content,
                    now,
                    expires_at,
                    target["token_hash"] if target else None,
                    target["username"] if target else None,
                    target["username_key"] if target else None,
                    session["username_key"],
                ),
            )
            connection.commit()
            saved = {
                "id": message_id,
                "senderName": session["username"],
                "kind": "text",
                "text": content,
                "createdAt": now,
                "expiresAt": expires_at,
            }
            if target:
                saved["recipientName"] = target["username"]
            return saved
        finally:
            connection.close()

    async def add_attachment_message(
        self,
        token_hash: str,
        media_id: str,
        message_id: str,
        recipient: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._add_attachment_message, token_hash, media_id, message_id, recipient
        )

    def _add_attachment_message(
        self,
        token_hash: str,
        media_id: str,
        message_id: str,
        recipient: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        now = time.time()
        expires_at = now + RETENTION_SECONDS
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT username, username_key FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
            if not session:
                connection.rollback()
                return None
            target = None
            if recipient:
                target = connection.execute(
                    """SELECT token_hash, username, username_key FROM sessions
                       WHERE token_hash = ? AND username_key = ? AND expires_at > ?""",
                    (recipient["token_hash"], recipient["username_key"], now),
                ).fetchone()
                if not target or target["token_hash"] == token_hash:
                    connection.rollback()
                    return None
            existing = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.message_id = ? AND messages.sender_key = ?""",
                (message_id, token_hash),
            ).fetchone()
            if existing:
                expected_recipient = target["token_hash"] if target else None
                if (
                    existing["recipient_key"] != expected_recipient
                    or existing["media_id"] != media_id
                ):
                    connection.rollback()
                    return None
                connection.rollback()
                return self._map_message_row(existing)
            media = connection.execute(
                """SELECT media_id, media_type, original_name, file_category, byte_size
                   FROM media
                   WHERE media_id = ? AND owner_key = ? AND expires_at > ?""",
                (media_id, token_hash, now),
            ).fetchone()
            if not media:
                connection.rollback()
                return None
            message_type = "image" if media["file_category"] == "image" else "file"
            connection.execute(
                """INSERT INTO messages
                   (message_id, sender_key, sender_name, message_type, text_content,
                    media_id, created_at, expires_at, recipient_key, recipient_name,
                    recipient_name_key, sender_name_key)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    token_hash,
                    session["username"],
                    message_type,
                    media_id,
                    now,
                    expires_at,
                    target["token_hash"] if target else None,
                    target["username"] if target else None,
                    target["username_key"] if target else None,
                    session["username_key"],
                ),
            )
            connection.execute(
                "UPDATE media SET expires_at = ? WHERE media_id = ?",
                (expires_at, media_id),
            )
            connection.commit()
            saved = {
                "id": message_id,
                "senderName": session["username"],
                "kind": message_type,
                "createdAt": now,
                "expiresAt": expires_at,
            }
            if message_type == "image":
                saved["mediaUrl"] = f"/media/{media_id}"
            else:
                saved.update(
                    {
                        "fileUrl": f"/media/{media_id}",
                        "fileName": media["original_name"],
                        "fileCategory": media["file_category"],
                        "fileSize": media["byte_size"],
                    }
                )
            if target:
                saved["recipientName"] = target["username"]
            return saved
        finally:
            connection.close()

    async def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._history, limit)

    def _history(self, limit: int) -> list[dict[str, Any]]:
        now = time.time()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.expires_at > ? AND messages.recipient_key IS NULL
                   ORDER BY messages.created_at DESC, messages.message_id DESC LIMIT ?""",
                (now, limit),
            ).fetchall()
            return self._map_history_rows(rows)
        finally:
            connection.close()

    async def public_history_page(self, limit: int = 100) -> dict[str, Any]:
        return await asyncio.to_thread(self._public_history_page, limit)

    def _public_history_page(self, limit: int) -> dict[str, Any]:
        now = time.time()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.expires_at > ? AND messages.recipient_key IS NULL
                   ORDER BY messages.created_at DESC, messages.message_id DESC LIMIT ?""",
                (now, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            return {
                "messages": self._map_history_rows(rows[:limit]),
                "hasMore": has_more,
            }
        finally:
            connection.close()

    async def private_history_page(
        self, token_hash: str, peer_name_key: str, limit: int = 100
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._private_history_page, token_hash, peer_name_key, limit
        )

    def _private_history_page(
        self, token_hash: str, peer_name_key: str, limit: int
    ) -> dict[str, Any]:
        now = time.time()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.expires_at > ? AND (
                       (messages.sender_key = ? AND messages.recipient_name_key = ?)
                       OR (messages.recipient_key = ? AND messages.sender_name_key = ?)
                   )
                   ORDER BY messages.created_at DESC, messages.message_id DESC LIMIT ?""",
                (now, token_hash, peer_name_key, token_hash, peer_name_key, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            return {
                "messages": self._map_history_rows(rows[:limit]),
                "hasMore": has_more,
            }
        finally:
            connection.close()

    async def private_history_before(
        self, token_hash: str, peer_name_key: str, message_id: str, limit: int = 100
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._private_history_before, token_hash, peer_name_key, message_id, limit
        )

    def _private_history_before(
        self, token_hash: str, peer_name_key: str, message_id: str, limit: int
    ) -> dict[str, Any]:
        now = time.time()
        connection = self._connect()
        try:
            anchor = connection.execute(
                """SELECT created_at, message_id FROM messages
                   WHERE message_id = ? AND expires_at > ? AND (
                       (sender_key = ? AND recipient_name_key = ?)
                       OR (recipient_key = ? AND sender_name_key = ?)
                   )""",
                (message_id, now, token_hash, peer_name_key, token_hash, peer_name_key),
            ).fetchone()
            if not anchor:
                return {"messages": [], "hasMore": False}
            rows = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.expires_at > ? AND (
                       (messages.sender_key = ? AND messages.recipient_name_key = ?)
                       OR (messages.recipient_key = ? AND messages.sender_name_key = ?)
                   ) AND (messages.created_at < ? OR
                          (messages.created_at = ? AND messages.message_id < ?))
                   ORDER BY messages.created_at DESC, messages.message_id DESC LIMIT ?""",
                (
                    now,
                    token_hash,
                    peer_name_key,
                    token_hash,
                    peer_name_key,
                    anchor["created_at"],
                    anchor["created_at"],
                    anchor["message_id"],
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            return {
                "messages": self._map_history_rows(rows[:limit]),
                "hasMore": has_more,
            }
        finally:
            connection.close()

    async def history_before(self, message_id: str, limit: int = 100) -> dict[str, Any]:
        return await asyncio.to_thread(self._history_before, message_id, limit)

    def _history_before(self, message_id: str, limit: int) -> dict[str, Any]:
        now = time.time()
        connection = self._connect()
        try:
            anchor = connection.execute(
                """SELECT created_at, message_id FROM messages
                   WHERE message_id = ? AND expires_at > ? AND recipient_key IS NULL""",
                (message_id, now),
            ).fetchone()
            if not anchor:
                return {"messages": [], "hasMore": False}
            rows = connection.execute(
                f"""SELECT {MESSAGE_COLUMNS} {MESSAGE_TABLE}
                   WHERE messages.expires_at > ? AND messages.recipient_key IS NULL
                     AND (messages.created_at < ? OR
                          (messages.created_at = ? AND messages.message_id < ?))
                   ORDER BY messages.created_at DESC, messages.message_id DESC LIMIT ?""",
                (
                    now,
                    anchor["created_at"],
                    anchor["created_at"],
                    anchor["message_id"],
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            return {
                "messages": self._map_history_rows(rows[:limit]),
                "hasMore": has_more,
            }
        finally:
            connection.close()

    @staticmethod
    def _map_message_row(row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "id": row["message_id"],
            "senderName": row["sender_name"],
            "kind": row["message_type"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
        }
        if "recipient_key" in row.keys() and row["recipient_key"]:
            item["recipientName"] = row["recipient_name"]
        if row["message_type"] == "text":
            item["text"] = row["text_content"]
        elif row["message_type"] == "image":
            item["mediaUrl"] = f"/media/{row['media_id']}"
        else:
            item.update(
                {
                    "fileUrl": f"/media/{row['media_id']}",
                    "fileName": row["original_name"] or "Shared file",
                    "fileCategory": row["file_category"] or "document",
                    "fileSize": row["attachment_size"] or 0,
                }
            )
        return item

    @staticmethod
    def _map_history_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [Store._map_message_row(row) for row in reversed(rows)]

    async def add_media(
        self,
        media_id: str,
        owner_key: str,
        file_path: Path,
        media_type: str,
        byte_size: int,
        original_name: str,
        file_category: str,
    ) -> None:
        await asyncio.to_thread(
            self._add_media,
            media_id,
            owner_key,
            str(file_path),
            media_type,
            byte_size,
            original_name,
            file_category,
        )

    def _add_media(
        self,
        media_id: str,
        owner_key: str,
        file_path: str,
        media_type: str,
        byte_size: int,
        original_name: str,
        file_category: str,
    ) -> None:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO media
                   (media_id, owner_key, file_path, media_type, byte_size,
                    original_name, file_category, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    media_id,
                    owner_key,
                    file_path,
                    media_type,
                    byte_size,
                    original_name,
                    file_category,
                    now,
                    now + RETENTION_SECONDS,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    async def media(self, media_id: str, token_hash: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._media, media_id, token_hash)

    def _media(self, media_id: str, token_hash: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT media.file_path, media.media_type, media.byte_size,
                          media.original_name, media.file_category
                   FROM media WHERE media.media_id = ? AND media.expires_at > ?
                     AND (media.owner_key = ? OR EXISTS (
                         SELECT 1 FROM messages
                         WHERE messages.media_id = media.media_id
                           AND messages.expires_at > ?
                           AND (messages.recipient_key IS NULL
                                OR messages.sender_key = ?
                                OR messages.recipient_key = ?)
                     ))""",
                (
                    media_id,
                    time.time(),
                    token_hash,
                    time.time(),
                    token_hash,
                    token_hash,
                ),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def expired_sessions(self) -> list[str]:
        return await asyncio.to_thread(self._expired_sessions)

    def _expired_sessions(self) -> list[str]:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT token_hash FROM sessions WHERE expires_at <= ?", (now,)
            ).fetchall()
            tokens = [row["token_hash"] for row in rows]
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute("DELETE FROM messages WHERE expires_at <= ?", (now,))
            connection.commit()
            return tokens
        finally:
            connection.close()

    async def expired_media(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._expired_media)

    def _expired_media(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT media_id, file_path FROM media WHERE expires_at <= ?""",
                (time.time(),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    async def forget_media(self, media_id: str) -> None:
        await asyncio.to_thread(self._forget_media, media_id)

    def _forget_media(self, media_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM media WHERE media_id = ? AND expires_at <= ?",
                (media_id, time.time()),
            )
            connection.commit()
        finally:
            connection.close()
