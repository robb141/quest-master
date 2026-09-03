"""Shared fixtures.

Every test gets its own database in a tmp_path, so the suite can never touch
the player's real ``quest.db`` — which the old ``test_client.py`` did on every
run, wiping the save it was supposedly smoke-testing.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from quest_master.db import Database, connect


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "quest.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    database = connect(db_path)
    yield database
    database.close()


@pytest.fixture
def rng() -> random.Random:
    """A fixed seed, so a test that asserts on combat gets the same fight every run."""
    return random.Random(1234)


@pytest.fixture
def legacy_db_path(tmp_path: Path) -> Path:
    """A database in the exact v0.1 shape: user_version 0, one hardcoded character."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE character (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            class TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            hp INTEGER NOT NULL,
            max_hp INTEGER NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0,
            gold INTEGER NOT NULL DEFAULT 10
        );
        CREATE TABLE inventory (item TEXT PRIMARY KEY, qty INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO character VALUES (1, 'Elowen', 'rogue', 3, 17, 26, 160, 42);
        INSERT INTO inventory VALUES ('rusty dagger', 1), ('healing potion', 2);
        INSERT INTO log (entry) VALUES ('Elowen the rogue begins their quest!');
        """
    )
    conn.commit()
    conn.close()
    return path
