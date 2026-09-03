"""The migration runner, including upgrading a real v0.1 save file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quest_master import engine
from quest_master.db import SCHEMA_VERSION, Database, connect


def test_fresh_database_is_at_head(db: Database) -> None:
    assert db.user_version == SCHEMA_VERSION


def test_migrate_is_idempotent(db: Database) -> None:
    assert db.migrate() == 0
    assert db.migrate() == 0
    assert db.user_version == SCHEMA_VERSION


def test_legacy_save_is_adopted_not_clobbered(legacy_db_path: Path) -> None:
    """A v0.1 quest.db keeps its character, gold, XP, inventory and log."""
    db = connect(legacy_db_path)
    try:
        assert db.user_version == SCHEMA_VERSION
        char = engine.character_view(db)
        assert char.name == "Elowen"
        assert char.level == 3
        assert char.gold == 42
        assert char.xp == 160
        assert char.hp == 17
        assert char.inventory == {"healing potion": 2, "rusty dagger": 1}
        # Columns added by migration 2 get sane defaults on an existing row.
        assert char.dead is False
        assert char.weapon == "rusty dagger"
        assert len(engine.log_entries(db)) == 1
    finally:
        db.close()


def test_legacy_save_becomes_the_active_save(legacy_db_path: Path) -> None:
    db = connect(legacy_db_path)
    try:
        saves = engine.list_characters(db)
        assert [s.name for s in saves.characters] == ["Elowen"]
        assert saves.characters[0].active is True
    finally:
        db.close()


def test_migration_2_drops_the_single_character_constraint(db: Database) -> None:
    """The v0.1 CHECK (id = 1) is what pinned the game to one save."""
    with db.reading() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'character'"
        ).fetchone()["sql"]
    assert "CHECK (id = 1)" not in sql


def test_new_tables_exist(db: Database) -> None:
    with db.reading() as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"character", "inventory", "log", "encounter", "active_save"} <= names


def test_failed_migration_leaves_version_untouched(tmp_path: Path) -> None:
    """A migration and its version bump commit together, or not at all."""
    path = tmp_path / "broken.db"
    db = Database(path)
    try:
        with pytest.raises(sqlite3.Error):
            db._conn.executescript(
                "BEGIN IMMEDIATE;\nCREATE TABLE t (a);\nSELECT nonexistent_fn();\n"
                "PRAGMA user_version = 99;\nCOMMIT;"
            )
        assert db.user_version == 0
    finally:
        db.close()


def test_transactions_roll_back_together(db: Database) -> None:
    """A turn that fails part-way leaves nothing behind."""
    engine.create_character(db, __import__("random").Random(1), "Bryn", "warrior")
    before = engine.character_view(db).gold
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("UPDATE character SET gold = 9999")
        conn.execute("INSERT INTO log (character_id, entry) VALUES (1, 'half a turn')")
        raise RuntimeError("boom")
    assert engine.character_view(db).gold == before
    assert not any("half a turn" in entry for _, entry in engine.log_entries(db))
