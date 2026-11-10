<<<<<<< ours
"""SQLite storage for generated test users."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from generators.user_generator import TestUser


class SQLiteManager:
    """Manages persistence of test users in a SQLite database."""

    def __init__(self, db_path: str = "output/test_users.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self.init_db()
        return self._conn

    def init_db(self) -> None:
        """Create the users table if it does not already exist."""
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                country TEXT NOT NULL,
                phone TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signup_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                success INTEGER NOT NULL,
                status_code INTEGER,
                error_message TEXT,
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def insert_user(self, user: TestUser) -> None:
        """Insert a single user, replacing any existing row with the same id."""
        conn = self.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO users
            (id, full_name, username, email, country, phone, password, created_at)
            VALUES (:id, :full_name, :username, :email, :country, :phone, :password, :created_at)
            """,
            user.to_dict(),
        )
        conn.commit()

    def insert_users(self, users: List[TestUser]) -> int:
        """Insert many users in a single transaction. Returns the number inserted."""
        conn = self.connect()
        conn.executemany(
            """
            INSERT OR REPLACE INTO users
            (id, full_name, username, email, country, phone, password, created_at)
            VALUES (:id, :full_name, :username, :email, :country, :phone, :password, :created_at)
            """,
            [user.to_dict() for user in users],
        )
        conn.commit()
        return len(users)

    def get_all_users(self) -> List[TestUser]:
        """Return all stored users ordered by creation time."""
        conn = self.connect()
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._row_to_user(row) for row in rows]

    def get_user_by_username(self, username: str) -> Optional[TestUser]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[TestUser]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def user_count(self) -> int:
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"]) if row else 0

    def delete_user(self, username: str) -> None:
        """Delete a user by username after successful submission."""
        conn = self.connect()
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()

    def save_signup_result(self, username: str, success: bool, status_code: int | None, error_message: str | None = None) -> None:
        """Save a signup result to the results table."""
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO signup_results (username, success, status_code, error_message, submitted_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (username, 1 if success else 0, status_code, error_message),
        )
        conn.commit()

    def delete_all_users(self) -> None:
        """Delete all users from the database."""
        conn = self.connect()
        conn.execute("DELETE FROM users")
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> TestUser:
        return TestUser(
            id=row["id"],
            full_name=row["full_name"],
            username=row["username"],
            email=row["email"],
            country=row["country"],
            phone=row["phone"],
            password=row["password"],
            created_at=row["created_at"],
        )

    def __enter__(self) -> "SQLiteManager":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
=======
"""SQLite storage for generated test users."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

from generators.user_generator import TestUser


class SQLiteManager:
    """Manages persistence of test users in a SQLite database."""

    def __init__(self, db_path: str = "output/test_users.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self.init_db()
        return self._conn

    def init_db(self) -> None:
        """Create the users table if it does not already exist."""
        conn = self.connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                country TEXT NOT NULL,
                phone TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def insert_user(self, user: TestUser) -> None:
        """Insert a single user, replacing any existing row with the same id."""
        conn = self.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO users
            (id, full_name, username, email, country, phone, password, created_at)
            VALUES (:id, :full_name, :username, :email, :country, :phone, :password, :created_at)
            """,
            user.to_dict(),
        )
        conn.commit()

    def insert_users(self, users: List[TestUser]) -> int:
        """Insert many users in a single transaction. Returns the number inserted."""
        conn = self.connect()
        conn.executemany(
            """
            INSERT OR REPLACE INTO users
            (id, full_name, username, email, country, phone, password, created_at)
            VALUES (:id, :full_name, :username, :email, :country, :phone, :password, :created_at)
            """,
            [user.to_dict() for user in users],
        )
        conn.commit()
        return len(users)

    def get_all_users(self) -> List[TestUser]:
        """Return all stored users ordered by creation time."""
        conn = self.connect()
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._row_to_user(row) for row in rows]

    def get_user_by_username(self, username: str) -> Optional[TestUser]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[TestUser]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def user_count(self) -> int:
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"]) if row else 0

    def delete_user(self, username: str) -> None:
        """Delete a user by username after successful submission."""
        conn = self.connect()
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()

    def delete_all_users(self) -> None:
        """Delete all users from the database."""
        conn = self.connect()
        conn.execute("DELETE FROM users")
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> TestUser:
        return TestUser(
            id=row["id"],
            full_name=row["full_name"],
            username=row["username"],
            email=row["email"],
            country=row["country"],
            phone=row["phone"],
            password=row["password"],
            created_at=row["created_at"],
        )

    def __enter__(self) -> "SQLiteManager":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
>>>>>>> theirs
