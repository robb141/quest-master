"""SQLite storage: one connection for the process, plus ordered schema migrations.

Two problems from the original single-file version are fixed here.

*One connection.* Every helper used to call ``sqlite3.connect`` for a single
query. The connection now lives for the life of the server (opened by the
lifespan hook in :mod:`quest_master.server`) and is shared. ``MCPServer`` runs
synchronous tool bodies on a worker thread pool, so the connection is opened
with ``check_same_thread=False`` and every access goes through a re-entrant
lock. That keeps the "one connection" property without handing a raw sqlite
connection to several threads at once.

*Migrations.* The old schema was created with bare ``CREATE TABLE IF NOT
EXISTS``, so any column added later silently never appeared in an existing
``quest.db``. Schema state is now tracked in ``PRAGMA user_version`` and moved
forward by the ordered :data:`MIGRATIONS` list. Each migration runs exactly
once, inside a transaction, in version order.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

# Migration 1 reproduces the original v0.1 schema verbatim. It is written with
# IF NOT EXISTS so that a quest.db created by the old server (which has
# user_version = 0 but already holds these tables) is adopted rather than
# clobbered: migration 1 becomes a no-op and migration 2 upgrades it in place.
_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS character (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    class TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    hp INTEGER NOT NULL,
    max_hp INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    gold INTEGER NOT NULL DEFAULT 10
);
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY,
    qty INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry TEXT NOT NULL,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

# Migration 2 turns the single hardcoded save into real save slots and adds the
# state the game needs to actually be a game: persistent encounters, a death
# flag, and an equipped weapon.
#
# The character table is rebuilt rather than ALTERed because SQLite cannot drop
# the old ``CHECK (id = 1)`` constraint, which is exactly what pinned the game
# to one character.
_MIGRATION_2 = """
CREATE TABLE character_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    class TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    hp INTEGER NOT NULL,
    max_hp INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    gold INTEGER NOT NULL DEFAULT 10,
    dead INTEGER NOT NULL DEFAULT 0,
    weapon TEXT NOT NULL DEFAULT 'rusty dagger',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO character_v2 (id, name, class, level, hp, max_hp, xp, gold)
    SELECT id, name, class, level, hp, max_hp, xp, gold FROM character;
DROP TABLE character;
ALTER TABLE character_v2 RENAME TO character;

CREATE TABLE inventory_v2 (
    character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    item TEXT NOT NULL COLLATE NOCASE,
    qty INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (character_id, item)
);
INSERT INTO inventory_v2 (character_id, item, qty)
    SELECT 1, item, qty FROM inventory
    WHERE EXISTS (SELECT 1 FROM character WHERE id = 1);
DROP TABLE inventory;
ALTER TABLE inventory_v2 RENAME TO inventory;

ALTER TABLE log ADD COLUMN character_id INTEGER;
UPDATE log SET character_id = 1
    WHERE EXISTS (SELECT 1 FROM character WHERE id = 1);
CREATE INDEX log_by_character ON log (character_id, id DESC);

CREATE TABLE encounter (
    character_id INTEGER PRIMARY KEY REFERENCES character(id) ON DELETE CASCADE,
    monster_key TEXT NOT NULL,
    enemy_name TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 1,
    hp INTEGER NOT NULL,
    max_hp INTEGER NOT NULL,
    rounds INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE active_save (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    character_id INTEGER REFERENCES character(id) ON DELETE SET NULL
);
INSERT INTO active_save (id, character_id)
    VALUES (1, (SELECT id FROM character ORDER BY id LIMIT 1));
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
)

SCHEMA_VERSION = MIGRATIONS[-1][0]


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


class Database:
    """A single long-lived SQLite connection, guarded by a lock.

    Use :meth:`transaction` for anything that writes: it holds the lock and
    commits (or rolls back) the whole block as one unit, so a game turn that
    updates the character *and* appends to the log can no longer half-succeed.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._depth = 0
        self._lock = threading.RLock()
        # isolation_level=None puts the driver in autocommit mode so transaction
        # boundaries are ours alone. sqlite3's legacy implicit-BEGIN machinery
        # interacts badly with executescript (which force-commits), and the
        # migration runner below depends on knowing exactly when COMMIT happens.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking on the writer; foreign keys are off by
        # default in SQLite and the schema leans on ON DELETE CASCADE.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def user_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Exclusive access for the duration of the block; commits on clean exit.

        Nesting is allowed and joins the outer transaction, so engine helpers
        can each declare a transaction without the caller having to care.
        """
        with self._lock:
            if self._depth > 0:  # already inside a transaction; join it
                self._depth += 1
                try:
                    yield self._conn
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._depth = 0

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """Shared-intent read access. Does not commit."""
        with self._lock:
            yield self._conn

    def migrate(self) -> int:
        """Apply any migrations newer than the stored ``user_version``.

        Returns the number of migrations applied.
        """
        applied = 0
        with self._lock:
            current = self.user_version
            for version, script in MIGRATIONS:
                if version <= current:
                    continue
                # The version bump rides inside the same script as the DDL so a
                # crash mid-migration leaves user_version untouched and the
                # migration is retried whole on the next start. PRAGMA does not
                # accept a bound parameter, and `version` comes from the
                # module-level MIGRATIONS tuple, never from user input.
                self._conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version:d};\nCOMMIT;"
                )
                applied += 1
        return applied

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def connect(path: Path | str) -> Database:
    """Open ``path`` and bring its schema up to date."""
    db = Database(path)
    db.migrate()
    return db
