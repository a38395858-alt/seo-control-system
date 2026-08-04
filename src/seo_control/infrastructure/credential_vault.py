"""Local encrypted credential vault kept outside PostgreSQL business schemas."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


class CredentialVault:
    def __init__(self, path: str | Path, *, import_database_path: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            with connection:
                connection.executescript(
                    """CREATE TABLE IF NOT EXISTS wordpress_credentials(
                           project_id INTEGER PRIMARY KEY,site_url TEXT NOT NULL,username TEXT NOT NULL,
                           application_password TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           last_tested_at TEXT
                       );
                       CREATE TABLE IF NOT EXISTS gsc_oauth_credentials(
                           id INTEGER PRIMARY KEY CHECK(id=1),account_email TEXT NOT NULL DEFAULT '',
                           refresh_token TEXT NOT NULL,scopes TEXT NOT NULL DEFAULT '',
                           created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                           updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                       );"""
                )
        finally:
            connection.close()
        if import_database_path is not None:
            self.import_existing(import_database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def import_existing(self, database_path: str | Path) -> None:
        source = sqlite3.connect(database_path)
        source.row_factory = sqlite3.Row
        target = self.connect()
        try:
            available_tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('project_wordpress_configs','gsc_oauth_connection')"
                ).fetchall()
            }
            with target:
                if "project_wordpress_configs" in available_tables:
                    for row in source.execute(
                        "SELECT project_id,site_url,username,application_password,updated_at,last_tested_at "
                        "FROM project_wordpress_configs WHERE trim(COALESCE(application_password,''))<>''"
                    ).fetchall():
                        target.execute(
                            """INSERT OR IGNORE INTO wordpress_credentials(
                                   project_id,site_url,username,application_password,updated_at,last_tested_at
                               ) VALUES(?,?,?,?,?,?)""",
                            tuple(row),
                        )
                if "gsc_oauth_connection" in available_tables:
                    row = source.execute(
                        "SELECT id,account_email,refresh_token,scopes,created_at,updated_at FROM gsc_oauth_connection "
                        "WHERE id=1 AND trim(COALESCE(refresh_token,''))<>''"
                    ).fetchone()
                    if row is not None:
                        target.execute(
                            "INSERT OR IGNORE INTO gsc_oauth_credentials(id,account_email,refresh_token,scopes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            tuple(row),
                        )
        finally:
            source.close()
            target.close()

    def readiness(self) -> dict[str, Any]:
        connection = self.connect()
        try:
            return {
                "wordpress_credentials": int(connection.execute("SELECT COUNT(*) FROM wordpress_credentials").fetchone()[0]),
                "gsc_oauth_credentials": int(connection.execute("SELECT COUNT(*) FROM gsc_oauth_credentials").fetchone()[0]),
                "path": str(self.path.resolve()),
            }
        finally:
            connection.close()
