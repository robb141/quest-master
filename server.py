"""
Quest Master — a tiny text-adventure/RPG engine exposed as an MCP server.

This one file demonstrates the three core building blocks of the Model
Context Protocol (MCP):

  1. TOOLS      -> functions the AI model can *call* to take action
                   (roll dice, attack a monster, rest to heal, ...)
  2. RESOURCES  -> read-only data the model can *fetch* for context
                   (the current character sheet, the game log)
  3. PROMPTS    -> reusable prompt templates the *user* can invoke
                   (e.g. "narrate this scene like a dungeon master")

Game state is persisted to a local SQLite file (quest.db) so your
character survives across sessions, just like a real save file.
"""

import random
import sqlite3
from contextlib import closing
from pathlib import Path

from mcp.server import MCPServer

DB_PATH = Path(__file__).parent / "quest.db"

# MCPServer is the high-level MCP server API: decorate a plain Python
# function with @mcp.tool() / @mcp.resource() / @mcp.prompt() and the
# SDK handles all the JSON-RPC/protocol plumbing for you.
mcp = MCPServer("quest-master")


# --------------------------------------------------------------------------
# Storage helpers
# --------------------------------------------------------------------------

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with closing(_db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS character (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                class TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                hp INTEGER NOT NULL,
                max_hp INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                gold INTEGER NOT NULL DEFAULT 10
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                item TEXT PRIMARY KEY,
                qty INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry TEXT NOT NULL,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _log(entry: str):
    with closing(_db()) as conn, conn:
        conn.execute("INSERT INTO log (entry) VALUES (?)", (entry,))


def _get_character():
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM character WHERE id = 1").fetchone()
        return dict(row) if row else None


_init_db()


# --------------------------------------------------------------------------
# TOOLS — things the model can *do*
# --------------------------------------------------------------------------

@mcp.tool()
def create_character(name: str, char_class: str) -> str:
    """Create a new adventurer and start a fresh quest.

    Overwrites any existing character. Choose a class such as
    'warrior', 'mage', 'rogue', or 'bard' — it only flavors the
    narration, all classes play the same mechanically.
    """
    max_hp = 20
    with closing(_db()) as conn, conn:
        conn.execute("DELETE FROM character")
        conn.execute("DELETE FROM inventory")
        conn.execute(
            "INSERT INTO character (id, name, class, level, hp, max_hp, xp, gold) "
            "VALUES (1, ?, ?, 1, ?, ?, 0, 10)",
            (name, char_class, max_hp, max_hp),
        )
        conn.execute("INSERT INTO inventory (item, qty) VALUES ('rusty dagger', 1)")
    _log(f"{name} the {char_class} begins their quest!")
    return f"🧙 {name} the {char_class} is created! HP {max_hp}/{max_hp}, 10 gold, armed with a rusty dagger."


@mcp.tool()
def roll_dice(sides: int = 20, count: int = 1) -> str:
    """Roll `count` dice with `sides` faces each (e.g. sides=20, count=1 for a d20 check).

    Use this for any skill check, saving throw, or random outcome
    when narrating the adventure.
    """
    if sides < 2 or count < 1 or count > 20:
        return "Invalid roll: sides must be >= 2 and count between 1 and 20."
    rolls = [random.randint(1, sides) for _ in range(count)]
    return f"🎲 Rolled {count}d{sides}: {rolls} (total: {sum(rolls)})"


@mcp.tool()
def attack(enemy_name: str, enemy_hp: int = 10) -> str:
    """Attack a named enemy in combat. Resolves one full exchange of blows.

    Rolls the character's attack, applies damage to the enemy, then
    the enemy strikes back if still alive. Updates the saved character HP,
    and awards gold/XP if the enemy is defeated.
    """
    char = _get_character()
    if not char:
        return "No character exists yet — call create_character first."

    player_roll = random.randint(1, 20) + char["level"]
    enemy_hp_remaining = max(0, enemy_hp - player_roll)
    narration = [f"⚔️ {char['name']} strikes {enemy_name} for {player_roll} damage "
                 f"({enemy_name} HP: {enemy_hp_remaining}/{enemy_hp})."]

    with closing(_db()) as conn, conn:
        if enemy_hp_remaining <= 0:
            gold_reward = random.randint(5, 15)
            xp_reward = random.randint(10, 25)
            new_xp = char["xp"] + xp_reward
            new_level = char["level"]
            level_up_msg = ""
            if new_xp >= new_level * 50:
                new_level += 1
                level_up_msg = f" 🌟 {char['name']} leveled up to level {new_level}!"
            conn.execute(
                "UPDATE character SET gold = gold + ?, xp = ?, level = ? WHERE id = 1",
                (gold_reward, new_xp, new_level),
            )
            narration.append(f"💀 {enemy_name} is defeated! +{gold_reward} gold, +{xp_reward} XP.{level_up_msg}")
        else:
            enemy_roll = random.randint(1, 12)
            new_hp = max(0, char["hp"] - enemy_roll)
            conn.execute("UPDATE character SET hp = ? WHERE id = 1", (new_hp,))
            narration.append(f"🩸 {enemy_name} hits back for {enemy_roll} damage "
                              f"({char['name']} HP: {new_hp}/{char['max_hp']}).")
            if new_hp == 0:
                narration.append(f"☠️ {char['name']} has fallen! Use rest() to recover before continuing.")

    _log(" ".join(narration))
    return " ".join(narration)


@mcp.tool()
def rest() -> str:
    """Rest at camp to fully restore HP."""
    char = _get_character()
    if not char:
        return "No character exists yet — call create_character first."
    with closing(_db()) as conn, conn:
        conn.execute("UPDATE character SET hp = max_hp WHERE id = 1")
    _log(f"{char['name']} rests and recovers to full health.")
    return f"🏕️ {char['name']} rests by the fire and is fully healed ({char['max_hp']}/{char['max_hp']} HP)."


@mcp.tool()
def add_item(item: str, qty: int = 1) -> str:
    """Add an item to the character's inventory (loot, purchases, quest rewards)."""
    with closing(_db()) as conn, conn:
        existing = conn.execute("SELECT qty FROM inventory WHERE item = ?", (item,)).fetchone()
        if existing:
            conn.execute("UPDATE inventory SET qty = qty + ? WHERE item = ?", (qty, item))
        else:
            conn.execute("INSERT INTO inventory (item, qty) VALUES (?, ?)", (item, qty))
    _log(f"Obtained {qty}x {item}.")
    return f"🎒 Added {qty}x {item} to inventory."


# --------------------------------------------------------------------------
# RESOURCES — read-only data the model can fetch for context
# --------------------------------------------------------------------------

@mcp.resource("quest://character")
def character_sheet() -> str:
    """The current character's full sheet: stats, inventory, and gold."""
    char = _get_character()
    if not char:
        return "No character created yet."

    with closing(_db()) as conn:
        items = conn.execute("SELECT item, qty FROM inventory").fetchall()

    lines = [
        f"# {char['name']} — Level {char['level']} {char['class']}",
        f"HP: {char['hp']}/{char['max_hp']}",
        f"XP: {char['xp']} (next level at {char['level'] * 50})",
        f"Gold: {char['gold']}",
        "",
        "## Inventory",
    ]
    lines += [f"- {row['item']} x{row['qty']}" for row in items] or ["- (empty)"]
    return "\n".join(lines)


@mcp.resource("quest://log")
def adventure_log() -> str:
    """The chronological log of everything that has happened this quest."""
    with closing(_db()) as conn:
        rows = conn.execute("SELECT entry, ts FROM log ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        return "The adventure has not yet begun."
    return "\n".join(f"[{r['ts']}] {r['entry']}" for r in rows)


# --------------------------------------------------------------------------
# PROMPTS — reusable templates the *user* can invoke in their chat client
# --------------------------------------------------------------------------

@mcp.prompt()
def narrate_scene(setting: str) -> str:
    """Ask the model to narrate the next scene of the adventure as a Dungeon Master."""
    return (
        f"You are an imaginative Dungeon Master running a tabletop RPG. "
        f"Check the current character sheet (quest://character) and adventure log "
        f"(quest://log) resources for context, then vividly narrate what happens "
        f"as the party enters: {setting}. Offer the player 2-3 concrete choices, "
        f"and use the roll_dice tool for any uncertain outcomes."
    )


if __name__ == "__main__":
    mcp.run()
