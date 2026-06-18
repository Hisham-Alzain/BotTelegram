"""
db.py — SQLite persistence for the archive bot menu tree.

Schema
------
menus   : tree of menu nodes (id, parent_id, label, position)
files   : files attached to a menu node (id, menu_id, file_id, caption, file_type, position)
admins  : dynamic admin list (user_id, username, added_by, confirmed)
          confirmed=0 -> pending (username stored, numeric ID not yet seen)
          confirmed=1 -> active (user has messaged bot, ID resolved)
"""

import sqlite3
import contextlib
from typing import Optional

DB_PATH = "archive.db"


# Connection helper


@contextlib.contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# Init


def init_db():
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS menus (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES menus(id) ON DELETE CASCADE,
                label     TEXT    NOT NULL,
                position  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS files (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_id   INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
                file_id   TEXT    NOT NULL,
                caption   TEXT    NOT NULL DEFAULT '',
                file_type TEXT    NOT NULL DEFAULT 'document',
                position  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admins (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER UNIQUE,
                username  TEXT    NOT NULL COLLATE NOCASE,
                added_by  INTEGER NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                added_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_menus_parent ON menus(parent_id);
            CREATE INDEX IF NOT EXISTS idx_files_menu   ON files(menu_id);
            CREATE INDEX IF NOT EXISTS idx_admins_uid   ON admins(user_id);
            CREATE INDEX IF NOT EXISTS idx_admins_uname ON admins(username);
        """)

    # Seed root menus if empty
    with conn() as c:
        root_count = c.execute(
            "SELECT COUNT(*) FROM menus WHERE parent_id IS NULL"
        ).fetchone()[0]
        if root_count == 0:
            c.executemany(
                "INSERT INTO menus (parent_id, label, position) VALUES (NULL, ?, ?)",
                [("🎵 الانشاد", 0), ("🎯 الانشطة", 1), ("📚 المواد العلمية", 2)],
            )


# Menu CRUD


def get_root_menus():
    with conn() as c:
        return c.execute(
            "SELECT * FROM menus WHERE parent_id IS NULL ORDER BY position, id"
        ).fetchall()


def get_children(parent_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM menus WHERE parent_id = ? ORDER BY position, id",
            (parent_id,),
        ).fetchall()


def get_menu(menu_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()


def create_menu(parent_id: Optional[int], label: str) -> int:
    with conn() as c:
        if parent_id is None:
            pos = c.execute(
                "SELECT COALESCE(MAX(position)+1, 0) FROM menus WHERE parent_id IS NULL"
            ).fetchone()[0]
        else:
            pos = c.execute(
                "SELECT COALESCE(MAX(position)+1, 0) FROM menus WHERE parent_id = ?",
                (parent_id,),
            ).fetchone()[0]
        cur = c.execute(
            "INSERT INTO menus (parent_id, label, position) VALUES (?, ?, ?)",
            (parent_id, label, pos),
        )
        return cur.lastrowid


def rename_menu(menu_id: int, new_label: str):
    with conn() as c:
        c.execute("UPDATE menus SET label = ? WHERE id = ?", (new_label, menu_id))


def delete_menu(menu_id: int):
    """Deletes menu and all descendants (CASCADE)."""
    with conn() as c:
        c.execute("DELETE FROM menus WHERE id = ?", (menu_id,))


# File CRUD


def get_files(menu_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM files WHERE menu_id = ? ORDER BY position, id",
            (menu_id,),
        ).fetchall()


def get_file(file_db_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM files WHERE id = ?", (file_db_id,)).fetchone()


def add_file(menu_id: int, file_id: str, caption: str, file_type: str) -> int:
    with conn() as c:
        pos = c.execute(
            "SELECT COALESCE(MAX(position)+1, 0) FROM files WHERE menu_id = ?",
            (menu_id,),
        ).fetchone()[0]
        cur = c.execute(
            "INSERT INTO files (menu_id, file_id, caption, file_type, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (menu_id, file_id, caption, file_type, pos),
        )
        return cur.lastrowid


def delete_file(file_db_id: int):
    with conn() as c:
        c.execute("DELETE FROM files WHERE id = ?", (file_db_id,))


# Breadcrumb helper


def get_breadcrumb(menu_id: int) -> list[str]:
    """Returns list of labels from root to this menu."""
    crumbs = []
    current = get_menu(menu_id)
    while current:
        crumbs.append(current["label"])
        pid = current["parent_id"]
        current = get_menu(pid) if pid else None
    return list(reversed(crumbs))


# Admin CRUD


def add_pending_admin(username: str, added_by: int) -> str:
    """
    Add a username as a pending admin (confirmed=0).
    Returns 'added', 'already_admin', or 'already_pending'.
    """
    username = username.lstrip("@").strip().lower()
    with conn() as c:
        existing = c.execute(
            "SELECT confirmed FROM admins WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            return "already_admin" if existing["confirmed"] else "already_pending"
        c.execute(
            "INSERT INTO admins (username, added_by, confirmed) VALUES (?, ?, 0)",
            (username, added_by),
        )
    return "added"


def confirm_admin(user_id: int, username: str) -> bool:
    """
    Called when any user messages the bot.
    If their username matches a pending admin row, confirm them.
    Returns True if they were just promoted to admin.
    """
    username = username.lower() if username else ""
    with conn() as c:
        row = c.execute(
            "SELECT id, confirmed FROM admins WHERE username = ? AND confirmed = 0",
            (username,),
        ).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE admins SET user_id = ?, confirmed = 1 WHERE id = ?",
            (user_id, row["id"]),
        )
    return True


def remove_admin(username: str) -> bool:
    """Remove an admin by username. Returns True if found and removed."""
    username = username.lstrip("@").strip().lower()
    with conn() as c:
        row = c.execute(
            "SELECT id FROM admins WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM admins WHERE username = ?", (username,))
    return True


def get_all_admins() -> list:
    """Return all admin rows (both confirmed and pending)."""
    with conn() as c:
        return c.execute(
            "SELECT username, user_id, confirmed, added_at FROM admins ORDER BY added_at"
        ).fetchall()


def is_db_admin(user_id: int) -> bool:
    """Check if a numeric user_id is a confirmed admin in the DB."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM admins WHERE user_id = ? AND confirmed = 1", (user_id,)
        ).fetchone()
        return row is not None
