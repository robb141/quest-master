"""Game rules. No MCP in this module.

Everything here takes an explicit :class:`~quest_master.db.Database` and an
explicit :class:`random.Random`, which is what makes combat testable: seed the
RNG and a fight plays out identically every run. The old version reached for
the unseeded ``random`` module global, so no combat path could be asserted on.

Each public function runs inside a single transaction, so a turn that updates
the character, decrements the encounter, and appends to the log either lands
whole or not at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from random import Random

from quest_master.content import (
    BESTIARY,
    CONSUMABLES,
    REST_FUEL,
    SHOP,
    STARTING_GOLD,
    STARTING_MAX_HP,
    STARTING_WEAPON,
    TRINKETS,
    WEAPONS,
    Monster,
    known_item,
    monster_from_encounter,
    resolve_monster,
    sell_price,
)
from quest_master.db import Database
from quest_master.models import (
    AttackResult,
    CharacterResult,
    CharacterView,
    DiceRoll,
    EncounterView,
    FleeResult,
    Rewards,
    SaveList,
    SaveSlot,
)

MAX_DICE = 20
MAX_NAME_LEN = 40


class QuestError(Exception):
    """A rules violation the player can correct (no character, dead, broke, ...).

    ``MCPServer`` turns a raised exception into an ``isError`` tool result, so
    these reach the model as genuine failures rather than as prose that happens
    to describe a failure.
    """


# --------------------------------------------------------------------------
# Progression maths
# --------------------------------------------------------------------------


def xp_threshold(level: int) -> int:
    """Total XP required to *be* ``level``. Level 2 at 50, 3 at 150, 4 at 300."""
    return 25 * level * (level - 1)


def player_armor(level: int) -> int:
    return 10 + level // 2


def _level_up_gain(rng: Random) -> int:
    return rng.randint(1, 8) + 2


# --------------------------------------------------------------------------
# Row access
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Char:
    id: int
    name: str
    char_class: str
    level: int
    hp: int
    max_hp: int
    xp: int
    gold: int
    dead: bool
    weapon: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> _Char:
        return cls(
            id=row["id"],
            name=row["name"],
            char_class=row["class"],
            level=row["level"],
            hp=row["hp"],
            max_hp=row["max_hp"],
            xp=row["xp"],
            gold=row["gold"],
            dead=bool(row["dead"]),
            weapon=row["weapon"],
        )


def _active(conn: sqlite3.Connection) -> _Char:
    row = conn.execute(
        "SELECT c.* FROM character c JOIN active_save a ON a.character_id = c.id WHERE a.id = 1"
    ).fetchone()
    if row is None:
        raise QuestError("No character exists yet — call create_character first.")
    return _Char.from_row(row)


def _living(conn: sqlite3.Connection) -> _Char:
    char = _active(conn)
    if char.dead:
        raise QuestError(
            f"{char.name} has fallen and cannot act. Call revive() to pay the shrine, "
            f"or create_character to start a new save."
        )
    return char


def _inventory(conn: sqlite3.Connection, character_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT item, qty FROM inventory WHERE character_id = ? AND qty > 0 ORDER BY item",
        (character_id,),
    ).fetchall()
    return {row["item"]: row["qty"] for row in rows}


def _log(conn: sqlite3.Connection, character_id: int, entry: str) -> None:
    conn.execute("INSERT INTO log (character_id, entry) VALUES (?, ?)", (character_id, entry))


def _give(conn: sqlite3.Connection, character_id: int, item: str, qty: int) -> None:
    conn.execute(
        "INSERT INTO inventory (character_id, item, qty) VALUES (?, ?, ?) "
        "ON CONFLICT (character_id, item) DO UPDATE SET qty = qty + excluded.qty",
        (character_id, item.strip().lower(), qty),
    )


def _take(conn: sqlite3.Connection, character_id: int, item: str, qty: int) -> None:
    """Remove ``qty`` of ``item``; raises if the character does not have that many."""
    name = item.strip().lower()
    held = conn.execute(
        "SELECT qty FROM inventory WHERE character_id = ? AND item = ?", (character_id, name)
    ).fetchone()
    if held is None or held["qty"] < qty:
        have = held["qty"] if held else 0
        raise QuestError(f"Not enough '{name}' in the pack (have {have}, need {qty}).")
    if held["qty"] == qty:
        conn.execute("DELETE FROM inventory WHERE character_id = ? AND item = ?", (character_id, name))
    else:
        conn.execute(
            "UPDATE inventory SET qty = qty - ? WHERE character_id = ? AND item = ?",
            (qty, character_id, name),
        )


def _view(conn: sqlite3.Connection, char: _Char) -> CharacterView:
    weapon = WEAPONS.get(char.weapon, WEAPONS[STARTING_WEAPON])
    return CharacterView(
        name=char.name,
        char_class=char.char_class,
        level=char.level,
        hp=char.hp,
        max_hp=char.max_hp,
        xp=char.xp,
        xp_to_next_level=xp_threshold(char.level + 1),
        gold=char.gold,
        weapon=char.weapon,
        weapon_damage=str(weapon.damage),
        armor=player_armor(char.level),
        dead=char.dead,
        inventory=_inventory(conn, char.id),
    )


def _reload(conn: sqlite3.Connection, character_id: int) -> _Char:
    row = conn.execute("SELECT * FROM character WHERE id = ?", (character_id,)).fetchone()
    assert row is not None
    return _Char.from_row(row)


# --------------------------------------------------------------------------
# Encounters
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Encounter:
    monster: Monster
    hp: int
    max_hp: int
    rounds: int


def _get_encounter(conn: sqlite3.Connection, character_id: int) -> _Encounter | None:
    row = conn.execute("SELECT * FROM encounter WHERE character_id = ?", (character_id,)).fetchone()
    if row is None:
        return None
    return _Encounter(
        monster=monster_from_encounter(row["monster_key"], row["enemy_name"], row["tier"]),
        hp=row["hp"],
        max_hp=row["max_hp"],
        rounds=row["rounds"],
    )


def _encounter_view(enc: _Encounter) -> EncounterView:
    return EncounterView(
        enemy_name=enc.monster.name,
        monster_key=enc.monster.key,
        hp=enc.hp,
        max_hp=enc.max_hp,
        tier=enc.monster.tier,
        rounds=enc.rounds,
    )


def _clear_encounter(conn: sqlite3.Connection, character_id: int) -> None:
    conn.execute("DELETE FROM encounter WHERE character_id = ?", (character_id,))


def _status(char: _Char, enc: _Encounter | None = None) -> str:
    """The status clause every narration ends with.

    Uniform shape and placement matter: the DM reads `narration` to write its
    prose, and when hit points appear in a different form per tool — or not at
    all — it either drops them or invents them. Ending every line the same way
    means the numbers are always there to quote.
    """
    parts = [f"{char.name} {char.hp}/{char.max_hp} HP"]
    if enc is not None and enc.hp > 0:
        parts.append(f"{enc.monster.name} {enc.hp}/{enc.max_hp} HP")
    return " — " + ", ".join(parts)


# --------------------------------------------------------------------------
# Character management
# --------------------------------------------------------------------------


def create_character(db: Database, rng: Random, name: str, char_class: str) -> CharacterResult:
    clean_name = name.strip()
    clean_class = char_class.strip().lower()
    if not clean_name:
        raise QuestError("A character needs a name.")
    if len(clean_name) > MAX_NAME_LEN:
        raise QuestError(f"Name must be {MAX_NAME_LEN} characters or fewer.")
    if not clean_class:
        raise QuestError("A character needs a class (warrior, mage, rogue, bard, ...).")

    with db.transaction() as conn:
        clash = conn.execute(
            "SELECT id FROM character WHERE name = ? COLLATE NOCASE", (clean_name,)
        ).fetchone()
        if clash is not None:
            raise QuestError(
                f"A save named '{clean_name}' already exists. Use switch_character to load it, "
                f"delete_character to remove it, or pick another name."
            )
        cursor = conn.execute(
            "INSERT INTO character (name, class, level, hp, max_hp, xp, gold, dead, weapon) "
            "VALUES (?, ?, 1, ?, ?, 0, ?, 0, ?)",
            (clean_name, clean_class, STARTING_MAX_HP, STARTING_MAX_HP, STARTING_GOLD, STARTING_WEAPON),
        )
        char_id = int(cursor.lastrowid or 0)
        _give(conn, char_id, STARTING_WEAPON, 1)
        _give(conn, char_id, REST_FUEL, 2)
        conn.execute("UPDATE active_save SET character_id = ? WHERE id = 1", (char_id,))
        _log(conn, char_id, f"{clean_name} the {clean_class} begins their quest!")
        char = _reload(conn, char_id)
        narration = (
            f"🧙 {clean_name} the {clean_class} takes up the quest with {STARTING_GOLD} gold, "
            f"a rusty dagger, and 2 rations.{_status(char)}"
        )
        return CharacterResult(narration=narration, character=_view(conn, char))


def list_characters(db: Database) -> SaveList:
    with db.reading() as conn:
        active = conn.execute("SELECT character_id FROM active_save WHERE id = 1").fetchone()
        active_id = active["character_id"] if active else None
        rows = conn.execute("SELECT * FROM character ORDER BY id").fetchall()
    slots = [
        SaveSlot(
            name=row["name"],
            char_class=row["class"],
            level=row["level"],
            hp=row["hp"],
            max_hp=row["max_hp"],
            dead=bool(row["dead"]),
            active=row["id"] == active_id,
        )
        for row in rows
    ]
    if not slots:
        return SaveList(narration="No saves yet — call create_character to begin.", characters=[])
    lines = [
        f"{'▶' if s.active else ' '} {s.name} — level {s.level} {s.char_class}, "
        f"HP {s.hp}/{s.max_hp}{' ☠️ fallen' if s.dead else ''}"
        for s in slots
    ]
    return SaveList(narration="📜 Saves:\n" + "\n".join(lines), characters=slots)


def switch_character(db: Database, name: str) -> CharacterResult:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM character WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        if row is None:
            raise QuestError(f"No save named '{name.strip()}'. Call list_characters to see them.")
        conn.execute("UPDATE active_save SET character_id = ? WHERE id = 1", (row["id"],))
        char = _Char.from_row(row)
        return CharacterResult(
            narration=(
                f"📖 Loaded {char.name}, level {char.level} {char.char_class}."
                f"{_status(char, _get_encounter(conn, char.id))}"
            ),
            character=_view(conn, char),
        )


def delete_character(db: Database, name: str) -> SaveList:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, name FROM character WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        if row is None:
            raise QuestError(f"No save named '{name.strip()}'.")
        # Inventory and encounter rows cascade; the log is kept but detached so
        # the chronicle of a finished run survives the save being deleted.
        conn.execute("UPDATE log SET character_id = NULL WHERE character_id = ?", (row["id"],))
        conn.execute("DELETE FROM character WHERE id = ?", (row["id"],))
        remaining = conn.execute("SELECT id FROM character ORDER BY id LIMIT 1").fetchone()
        conn.execute(
            "UPDATE active_save SET character_id = ? WHERE id = 1",
            (remaining["id"] if remaining else None,),
        )
    result = list_characters(db)
    return SaveList(
        narration=f"🗑️ Deleted save '{row['name']}'.\n{result.narration}", characters=result.characters
    )


def revive(db: Database) -> CharacterResult:
    with db.transaction() as conn:
        char = _active(conn)
        if not char.dead:
            raise QuestError(f"{char.name} is alive and does not need reviving.")
        toll = max(5, char.gold // 2)
        paid = min(toll, char.gold)
        restored = max(1, char.max_hp // 2)
        conn.execute(
            "UPDATE character SET dead = 0, hp = ?, gold = gold - ? WHERE id = ?",
            (restored, paid, char.id),
        )
        _clear_encounter(conn, char.id)
        revived = _reload(conn, char.id)
        narration = (
            f"⛩️ The shrine takes {paid} gold and breathes life back into {char.name}.{_status(revived)}"
        )
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, revived))


# --------------------------------------------------------------------------
# Dice
# --------------------------------------------------------------------------


def roll_dice(rng: Random, sides: int = 20, count: int = 1) -> DiceRoll:
    if sides < 2:
        raise QuestError(f"A die needs at least 2 sides (got {sides}).")
    if not 1 <= count <= MAX_DICE:
        raise QuestError(f"Roll between 1 and {MAX_DICE} dice at a time (got {count}).")
    rolls = [rng.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    return DiceRoll(
        narration=f"🎲 Rolled {count}d{sides}: {rolls} (total: {total})",
        notation=f"{count}d{sides}",
        rolls=rolls,
        total=total,
    )


# --------------------------------------------------------------------------
# Combat
# --------------------------------------------------------------------------


def _apply_level_ups(conn: sqlite3.Connection, rng: Random, char: _Char) -> tuple[int, list[str]]:
    """Grant every level the character's XP now entitles them to.

    The original checked a single ``if``, so a big kill could only ever award
    one level, and it raised ``level`` without touching ``max_hp`` — levelling
    was almost purely cosmetic.
    """
    gained = 0
    lines: list[str] = []
    while char.xp >= xp_threshold(char.level + 1):
        char.level += 1
        char.max_hp += _level_up_gain(rng)
        gained += 1
        lines.append(f"🌟 {char.name} reaches level {char.level}! Max HP is now {char.max_hp}.")
    if gained:
        char.hp = char.max_hp
        conn.execute(
            "UPDATE character SET level = ?, max_hp = ?, hp = ? WHERE id = ?",
            (char.level, char.max_hp, char.hp, char.id),
        )
    return gained, lines


def attack(db: Database, rng: Random, enemy_name: str | None = None) -> AttackResult:
    """Resolve one exchange of blows against the enemy currently being fought.

    The enemy's HP lives in the ``encounter`` table. The old signature took
    ``enemy_hp`` as an argument on every call, so the model re-supplied a fresh
    value each turn and the monster silently healed between exchanges; there was
    no way to lose a fight.
    """
    with db.transaction() as conn:
        char = _living(conn)
        enc = _get_encounter(conn, char.id)
        lines: list[str] = []

        if enc is None:
            if not enemy_name or not enemy_name.strip():
                raise QuestError("No fight is in progress — call attack with enemy_name to start one.")
            monster = resolve_monster(enemy_name, char.level)
            enc = _Encounter(monster=monster, hp=monster.hp, max_hp=monster.hp, rounds=0)
            conn.execute(
                "INSERT INTO encounter (character_id, monster_key, enemy_name, tier, hp, max_hp, rounds) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (char.id, monster.key, monster.name, monster.tier, enc.hp, enc.max_hp),
            )
            lines.append(f"⚔️ {char.name} closes with the {monster.name}.")
        elif enemy_name and enemy_name.strip().lower() != enc.monster.name.lower():
            lines.append(
                f"(Still locked in combat with the {enc.monster.name}; "
                f"break away before facing {enemy_name.strip()}.)"
            )

        monster = enc.monster
        weapon = WEAPONS.get(char.weapon, WEAPONS[STARTING_WEAPON])

        # --- the player's swing -------------------------------------------
        d20 = rng.randint(1, 20)
        critical = d20 == 20
        fumble = d20 == 1
        attack_roll = d20 + char.level
        hit = critical or (not fumble and attack_roll >= monster.armor)

        damage = 0
        if hit:
            damage = weapon.damage.roll(rng) + (char.level + 1) // 2
            if critical:
                damage *= 2
                lines.append(f"💥 Critical! {char.name}'s {char.weapon} bites deep for {damage} damage.")
            else:
                lines.append(f"🗡️ {char.name} hits the {monster.name} for {damage} damage.")
        else:
            lines.append(
                f"🌀 {char.name} swings and misses (rolled {attack_roll} vs armour {monster.armor})."
            )

        enemy_hp = max(0, enc.hp - damage)
        defeated = enemy_hp == 0
        rewards: Rewards | None = None
        levels_gained = 0
        counter_hit = False
        taken = 0
        died = False

        if defeated:
            xp = rng.randint(*monster.xp)
            gold = rng.randint(*monster.gold)
            loot: list[str] = []
            if monster.loot and rng.random() < monster.loot_chance:
                loot.append(rng.choice(monster.loot))
            char.xp += xp
            char.gold += gold
            conn.execute("UPDATE character SET xp = ?, gold = ? WHERE id = ?", (char.xp, char.gold, char.id))
            for item in loot:
                _give(conn, char.id, item, 1)
            _clear_encounter(conn, char.id)
            rewards = Rewards(xp=xp, gold=gold, loot=loot)
            lines.append(f"💀 The {monster.name} falls! +{xp} XP, +{gold} gold.")
            if loot:
                lines.append(f"🎁 Looted: {', '.join(loot)}.")
            levels_gained, level_lines = _apply_level_ups(conn, rng, char)
            lines.extend(level_lines)
        else:
            conn.execute(
                "UPDATE encounter SET hp = ?, rounds = rounds + 1 WHERE character_id = ?",
                (enemy_hp, char.id),
            )
            enc.hp = enemy_hp
            enc.rounds += 1

            # --- the enemy's reply ----------------------------------------
            enemy_roll = rng.randint(1, 20) + monster.attack_bonus
            counter_hit = enemy_roll >= player_armor(char.level)
            if counter_hit:
                taken = monster.damage.roll(rng)
                char.hp = max(0, char.hp - taken)
                lines.append(f"🩸 The {monster.name} strikes back for {taken}.")
            else:
                lines.append(f"🛡️ The {monster.name} lunges and {char.name} turns the blow aside.")

            died = char.hp == 0
            conn.execute(
                "UPDATE character SET hp = ?, dead = ? WHERE id = ?",
                (char.hp, int(died), char.id),
            )
            if died:
                _clear_encounter(conn, char.id)
                lines.append(f"☠️ {char.name} falls to the {monster.name}. Only the shrine can help now.")

        fresh = _reload(conn, char.id)
        narration = " ".join(lines) + _status(fresh, None if (defeated or died) else enc)
        _log(conn, char.id, narration)
        return AttackResult(
            narration=narration,
            attack_roll=attack_roll,
            hit=hit,
            critical=critical,
            damage_dealt=damage,
            enemy_defeated=defeated,
            counterattack_hit=counter_hit,
            damage_taken=taken,
            character_died=died,
            levels_gained=levels_gained,
            rewards=rewards,
            encounter=None if (defeated or died) else _encounter_view(enc),
            character=_view(conn, fresh),
        )


def flee(db: Database, rng: Random) -> FleeResult:
    """Try to break off the current fight. Failure costs a parting blow."""
    with db.transaction() as conn:
        char = _living(conn)
        enc = _get_encounter(conn, char.id)
        if enc is None:
            raise QuestError("There is nothing to flee from.")
        monster = enc.monster
        roll = rng.randint(1, 20) + char.level
        target = 10 + monster.tier
        escaped = roll >= target
        taken = 0
        died = False
        if escaped:
            _clear_encounter(conn, char.id)
            narration = f"🏃 {char.name} breaks away from the {monster.name} and slips into the dark."
        else:
            taken = monster.damage.roll(rng)
            char.hp = max(0, char.hp - taken)
            died = char.hp == 0
            conn.execute("UPDATE character SET hp = ?, dead = ? WHERE id = ?", (char.hp, int(died), char.id))
            conn.execute("UPDATE encounter SET rounds = rounds + 1 WHERE character_id = ?", (char.id,))
            narration = f"🚫 The {monster.name} cuts off the retreat and lands {taken} damage."
            if died:
                _clear_encounter(conn, char.id)
                narration += f" ☠️ {char.name} falls. Only the shrine can help now."
        fled = _reload(conn, char.id)
        narration += _status(fled, None if (escaped or died) else _get_encounter(conn, char.id))
        _log(conn, char.id, narration)
        return FleeResult(
            narration=narration,
            escaped=escaped,
            damage_taken=taken,
            character_died=died,
            character=_view(conn, fled),
        )


# --------------------------------------------------------------------------
# Camp, inventory, trade
# --------------------------------------------------------------------------


def rest(db: Database, rng: Random) -> CharacterResult:
    """Make camp. A ration buys a full heal; going hungry buys very little.

    ``rest`` used to be a free, unlimited full heal usable mid-fight, which
    removed every reason to lose.
    """
    with db.transaction() as conn:
        char = _living(conn)
        if _get_encounter(conn, char.id) is not None:
            raise QuestError("You cannot make camp mid-fight — win the fight or flee() first.")
        if char.hp >= char.max_hp:
            raise QuestError(f"{char.name} is already at full health ({char.hp}/{char.max_hp}).")

        held = _inventory(conn, char.id)
        if held.get(REST_FUEL, 0) > 0:
            _take(conn, char.id, REST_FUEL, 1)
            conn.execute("UPDATE character SET hp = max_hp WHERE id = ?", (char.id,))
            narration = f"🏕️ {char.name} eats a ration and sleeps soundly, waking whole."
        else:
            healed = max(1, char.max_hp // 4)
            new_hp = min(char.max_hp, char.hp + healed)
            conn.execute("UPDATE character SET hp = ? WHERE id = ?", (new_hp, char.id))
            narration = (
                f"🥣 No rations left. {char.name} sleeps hungry and recovers only "
                f"{new_hp - char.hp} HP. The shop sells rations."
            )
        rested = _reload(conn, char.id)
        narration += _status(rested)
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, rested))


def add_item(db: Database, item: str, qty: int = 1) -> CharacterResult:
    if qty < 1:
        raise QuestError("Quantity must be at least 1.")
    name = item.strip().lower()
    if not name:
        raise QuestError("An item needs a name.")
    with db.transaction() as conn:
        char = _active(conn)
        _give(conn, char.id, name, qty)
        note = "" if known_item(name) else " (a curio with no mechanical effect)"
        stocked = _reload(conn, char.id)
        narration = f"🎒 Added {qty}x {name} to {char.name}'s pack{note}.{_status(stocked)}"
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, stocked))


def use_item(db: Database, rng: Random, item: str) -> CharacterResult:
    """Consume a potion or elixir. This is what finally makes loot matter."""
    name = item.strip().lower()
    consumable = CONSUMABLES.get(name)
    if consumable is None:
        if name in WEAPONS:
            raise QuestError(f"'{name}' is a weapon — call equip('{name}') instead.")
        if name in TRINKETS:
            raise QuestError(f"'{name}' is a trinket. It can be sold, not used.")
        raise QuestError(f"'{name}' is not something you can use. Check quest://shop for what is.")
    if consumable.is_rest_fuel:
        raise QuestError("Rations are eaten by rest(), not used directly.")

    with db.transaction() as conn:
        char = _living(conn)
        _take(conn, char.id, name, 1)
        parts: list[str] = []
        if consumable.max_hp_bonus:
            char.max_hp += consumable.max_hp_bonus
            char.hp += consumable.max_hp_bonus
            parts.append(f"max HP rises to {char.max_hp}")
        if consumable.heal is not None:
            healed = consumable.heal.roll(rng)
            before = char.hp
            char.hp = min(char.max_hp, char.hp + healed)
            parts.append(f"recovers {char.hp - before} HP")
        conn.execute("UPDATE character SET hp = ?, max_hp = ? WHERE id = ?", (char.hp, char.max_hp, char.id))
        used = _reload(conn, char.id)
        narration = f"🧪 {char.name} uses the {name} and {', '.join(parts)}.{_status(used)}"
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, used))


def equip(db: Database, item: str) -> CharacterResult:
    """Wield a weapon from the pack. The equipped weapon sets your damage dice."""
    name = item.strip().lower()
    weapon = WEAPONS.get(name)
    if weapon is None:
        raise QuestError(f"'{name}' is not a weapon. Known weapons: {', '.join(sorted(WEAPONS))}.")
    with db.transaction() as conn:
        char = _living(conn)
        if _inventory(conn, char.id).get(name, 0) < 1:
            raise QuestError(f"{char.name} is not carrying a {name}.")
        if char.weapon == name:
            raise QuestError(f"{char.name} is already wielding the {name}.")
        conn.execute("UPDATE character SET weapon = ? WHERE id = ?", (name, char.id))
        armed = _reload(conn, char.id)
        narration = (
            f"🗡️ {char.name} draws the {name} ({weapon.damage} damage). "
            f"{weapon.description}{_status(armed, _get_encounter(conn, char.id))}"
        )
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, armed))


def buy(db: Database, item: str, qty: int = 1) -> CharacterResult:
    """Spend gold at the shop. Gold had no sink at all before this."""
    if qty < 1:
        raise QuestError("Quantity must be at least 1.")
    name = item.strip().lower()
    price = SHOP.get(name)
    if price is None:
        raise QuestError(f"The shop does not stock '{name}'. See quest://shop for the catalogue.")
    cost = price * qty
    with db.transaction() as conn:
        char = _living(conn)
        if char.gold < cost:
            raise QuestError(f"{qty}x {name} costs {cost} gold; {char.name} has {char.gold}.")
        conn.execute("UPDATE character SET gold = gold - ? WHERE id = ?", (cost, char.id))
        _give(conn, char.id, name, qty)
        bought = _reload(conn, char.id)
        narration = f"🪙 Bought {qty}x {name} for {cost} gold ({bought.gold} left).{_status(bought)}"
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, bought))


def sell(db: Database, item: str, qty: int = 1) -> CharacterResult:
    if qty < 1:
        raise QuestError("Quantity must be at least 1.")
    name = item.strip().lower()
    with db.transaction() as conn:
        char = _living(conn)
        if name == char.weapon:
            raise QuestError(f"{char.name} is wielding the {name}; equip something else first.")
        _take(conn, char.id, name, qty)
        payout = sell_price(name) * qty
        conn.execute("UPDATE character SET gold = gold + ? WHERE id = ?", (payout, char.id))
        sold = _reload(conn, char.id)
        narration = f"🪙 Sold {qty}x {name} for {payout} gold ({sold.gold} total).{_status(sold)}"
        _log(conn, char.id, narration)
        return CharacterResult(narration=narration, character=_view(conn, sold))


# --------------------------------------------------------------------------
# Read models used by resources
# --------------------------------------------------------------------------


def character_view(db: Database, name: str | None = None) -> CharacterView:
    with db.reading() as conn:
        if name is None:
            char = _active(conn)
        else:
            row = conn.execute(
                "SELECT * FROM character WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
            if row is None:
                raise QuestError(f"No save named '{name.strip()}'.")
            char = _Char.from_row(row)
        return _view(conn, char)


def encounter_view(db: Database) -> EncounterView | None:
    with db.reading() as conn:
        char = _active(conn)
        enc = _get_encounter(conn, char.id)
        return _encounter_view(enc) if enc else None


def log_entries(db: Database, limit: int = 50) -> list[tuple[str, str]]:
    """Most recent first, scoped to the active save.

    The old log was global, so ``create_character`` — which wiped the character
    and inventory but not the log — left every new run reading the previous
    run's history.
    """
    with db.reading() as conn:
        char_id = conn.execute("SELECT character_id FROM active_save WHERE id = 1").fetchone()
        if char_id is None or char_id["character_id"] is None:
            return []
        rows = conn.execute(
            "SELECT entry, ts FROM log WHERE character_id = ? ORDER BY id DESC LIMIT ?",
            (char_id["character_id"], limit),
        ).fetchall()
        return [(row["ts"], row["entry"]) for row in rows]


def bestiary_entries() -> list[Monster]:
    return sorted(BESTIARY.values(), key=lambda m: (m.tier, m.key))


def resolve_for_description(db: Database, enemy_name: str) -> Monster:
    """Look up what an enemy name resolves to, without starting a fight.

    If a fight is already underway against that enemy, the stored encounter's
    monster wins, so `describe_enemy` can never report different stats from the
    ones the fight is actually using.
    """
    name = enemy_name.strip()
    if not name:
        raise QuestError("enemy_name must not be empty.")
    level = 1
    with db.reading() as conn:
        try:
            char = _active(conn)
        except QuestError:
            return resolve_monster(name, level)
        level = char.level
        enc = _get_encounter(conn, char.id)
        if enc is not None and enc.monster.name.lower() == name.lower():
            return enc.monster
    return resolve_monster(name, level)
