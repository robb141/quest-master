"""Game rules, with a seeded RNG so every fight is reproducible.

The tests named after a bug are regression guards for the specific defects the
v0.1 engine shipped with.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from quest_master import engine
from quest_master.content import BESTIARY, REST_FUEL, WEAPONS
from quest_master.db import Database, connect
from quest_master.engine import QuestError, xp_threshold


def hero(db: Database, rng: random.Random, name: str = "Elowen", char_class: str = "rogue") -> None:
    engine.create_character(db, rng, name, char_class)


def set_state(db: Database, **fields: object) -> None:
    """Force character columns, for setting up a specific situation."""
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as conn:
        char_id = conn.execute("SELECT character_id FROM active_save WHERE id = 1").fetchone()[0]
        conn.execute(f"UPDATE character SET {assignments} WHERE id = ?", (*fields.values(), char_id))


# --------------------------------------------------------------------------
# Creation and save slots
# --------------------------------------------------------------------------


def test_create_character_starts_a_new_hero(db: Database, rng: random.Random) -> None:
    result = engine.create_character(db, rng, "Elowen", "Rogue")
    assert result.character.name == "Elowen"
    assert result.character.char_class == "rogue"
    assert result.character.hp == result.character.max_hp == 20
    assert result.character.gold == 10
    assert result.character.inventory == {"rusty dagger": 1, "ration": 2}


@pytest.mark.parametrize("name,char_class", [("", "rogue"), ("   ", "mage"), ("Elowen", "")])
def test_create_character_rejects_blanks(
    db: Database, rng: random.Random, name: str, char_class: str
) -> None:
    with pytest.raises(QuestError):
        engine.create_character(db, rng, name, char_class)


def test_saves_coexist_and_switch(db: Database, rng: random.Random) -> None:
    """v0.1 hardcoded id = 1, so a second character destroyed the first."""
    engine.create_character(db, rng, "Elowen", "rogue")
    engine.create_character(db, rng, "Bryn", "warrior")
    assert engine.character_view(db).name == "Bryn"

    engine.switch_character(db, "elowen")  # names match case-insensitively
    assert engine.character_view(db).name == "Elowen"

    saves = engine.list_characters(db)
    assert {s.name for s in saves.characters} == {"Elowen", "Bryn"}
    assert [s.name for s in saves.characters if s.active] == ["Elowen"]


def test_duplicate_save_name_is_refused(db: Database, rng: random.Random) -> None:
    engine.create_character(db, rng, "Elowen", "rogue")
    with pytest.raises(QuestError, match="already exists"):
        engine.create_character(db, rng, "elowen", "mage")


def test_new_character_does_not_inherit_the_previous_log(db: Database, rng: random.Random) -> None:
    """BUG (v0.1): create_character wiped character and inventory but not the log,
    so every new run began already showing the last run's history."""
    engine.create_character(db, rng, "Elowen", "rogue")
    engine.attack(db, rng, "rat")
    assert len(engine.log_entries(db)) >= 2

    engine.create_character(db, rng, "Bryn", "warrior")
    entries = [entry for _, entry in engine.log_entries(db)]
    assert len(entries) == 1
    assert "Bryn" in entries[0]
    assert not any("Elowen" in e for e in entries)


def test_delete_character_promotes_another_save(db: Database, rng: random.Random) -> None:
    engine.create_character(db, rng, "Elowen", "rogue")
    engine.create_character(db, rng, "Bryn", "warrior")
    engine.delete_character(db, "Bryn")
    assert engine.character_view(db).name == "Elowen"
    with pytest.raises(QuestError):
        engine.character_view(db, "Bryn")


def test_no_character_is_an_error_not_a_message(db: Database) -> None:
    """v0.1 returned the string 'No character exists yet' as a *successful* result."""
    with pytest.raises(QuestError, match="No character exists"):
        engine.character_view(db)


# --------------------------------------------------------------------------
# Dice
# --------------------------------------------------------------------------


def test_roll_dice_is_in_range(rng: random.Random) -> None:
    roll = engine.roll_dice(rng, sides=6, count=4)
    assert roll.notation == "4d6"
    assert len(roll.rolls) == 4
    assert all(1 <= r <= 6 for r in roll.rolls)
    assert roll.total == sum(roll.rolls)


@pytest.mark.parametrize("sides,count", [(1, 1), (0, 1), (20, 0), (20, 21), (20, -3)])
def test_roll_dice_raises_on_invalid_input(rng: random.Random, sides: int, count: int) -> None:
    """v0.1 returned 'Invalid roll: ...' as a normal result, so the model could
    not tell a bad roll from a real one."""
    with pytest.raises(QuestError):
        engine.roll_dice(rng, sides=sides, count=count)


# --------------------------------------------------------------------------
# Combat
# --------------------------------------------------------------------------


def test_enemy_hp_persists_between_attacks(db: Database, rng: random.Random) -> None:
    """BUG (v0.1): enemy_hp was an argument on every call, so the enemy was
    restored to full health each turn and no fight could ever be won or lost."""
    hero(db, rng)
    first = engine.attack(db, rng, "ogre")  # 60 HP; will not die in one round
    assert first.encounter is not None
    assert first.encounter.hp < first.encounter.max_hp

    second = engine.attack(db, rng)  # no enemy_name — continue the same fight
    assert second.encounter is not None
    assert second.encounter.hp <= first.encounter.hp
    assert second.encounter.rounds == 2


def test_attack_without_a_fight_and_without_a_name_is_an_error(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="No fight is in progress"):
        engine.attack(db, rng)


def test_naming_a_different_enemy_mid_fight_does_not_switch_targets(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    engine.attack(db, rng, "ogre")
    result = engine.attack(db, rng, "rat")
    assert result.encounter is not None
    assert result.encounter.monster_key == "ogre"
    assert "Still locked in combat" in result.narration


def test_defeating_an_enemy_ends_the_encounter_and_pays_out(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    result = engine.attack(db, rng, "rat")
    for _ in range(30):
        if result.enemy_defeated:
            break
        result = engine.attack(db, rng)
    assert result.enemy_defeated
    assert result.encounter is None
    assert result.rewards is not None
    assert result.rewards.xp >= BESTIARY["rat"].xp[0]
    assert engine.encounter_view(db) is None


def test_a_character_can_die_and_then_cannot_act(db: Database, rng: random.Random) -> None:
    """v0.1 let you keep attacking forever at 0 HP; death was not a state."""
    hero(db, rng)
    set_state(db, hp=1)
    result = engine.attack(db, rng, "dragon")
    for _ in range(40):
        if result.character_died:
            break
        result = engine.attack(db, rng)
    assert result.character_died
    assert result.character.dead is True
    assert result.character.hp == 0
    assert engine.encounter_view(db) is None

    with pytest.raises(QuestError, match="has fallen"):
        engine.attack(db, rng)
    with pytest.raises(QuestError, match="has fallen"):
        engine.rest(db, rng)


def test_revive_costs_half_your_gold(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    set_state(db, hp=0, dead=1, gold=100)
    result = engine.revive(db)
    assert result.character.dead is False
    assert result.character.gold == 50
    assert result.character.hp == result.character.max_hp // 2


def test_revive_refuses_a_living_character(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="does not need reviving"):
        engine.revive(db)


def test_combat_is_deterministic_under_a_fixed_seed(tmp_path: Path) -> None:
    """The old engine used the unseeded `random` module global, so no combat
    outcome could be asserted on at all."""

    def play(run: int) -> list[str]:
        database = connect(tmp_path / f"run{run}.db")
        try:
            seeded = random.Random(99)
            engine.create_character(database, seeded, "Elowen", "rogue")
            narrations = []
            result = engine.attack(database, seeded, "goblin")
            for _ in range(10):
                narrations.append(result.narration)
                if result.enemy_defeated or result.character_died:
                    break
                result = engine.attack(database, seeded)
            return narrations
        finally:
            database.close()

    first, second = play(1), play(2)
    assert first == second
    assert len(first) > 1  # the fight actually ran for several rounds


# --------------------------------------------------------------------------
# Progression
# --------------------------------------------------------------------------


def test_xp_thresholds_climb(db: Database) -> None:
    assert xp_threshold(1) == 0
    assert xp_threshold(2) == 50
    assert xp_threshold(3) == 150
    assert xp_threshold(2) < xp_threshold(3) < xp_threshold(4)


def test_level_up_raises_max_hp_and_heals(db: Database, rng: random.Random) -> None:
    """BUG (v0.1): levelling bumped `level` and nothing else — max_hp never grew."""
    hero(db, rng)
    set_state(db, xp=xp_threshold(2) - 1, hp=5)
    result = engine.attack(db, rng, "rat")
    for _ in range(30):
        if result.levels_gained:
            break
        result = engine.attack(db, rng)
    assert result.levels_gained >= 1
    assert result.character.level >= 2
    assert result.character.max_hp > 20
    assert result.character.hp == result.character.max_hp


def test_a_single_kill_can_grant_several_levels(db: Database, rng: random.Random) -> None:
    """BUG (v0.1): a lone `if` meant one kill could never award more than one level."""
    hero(db, rng)
    # Enough HP to survive the fight, and one XP short of level 6 so that a
    # dragon's payout must cross several thresholds at once.
    set_state(db, xp=xp_threshold(6) - 1, level=1, max_hp=9999, hp=9999)
    result = engine.attack(db, rng, "dragon")
    for _ in range(400):
        if result.enemy_defeated:
            break
        result = engine.attack(db, rng)
    assert result.enemy_defeated
    assert result.levels_gained >= 2
    assert result.character.level >= 6


# --------------------------------------------------------------------------
# Camp and resources
# --------------------------------------------------------------------------


def test_rest_eats_a_ration_for_a_full_heal(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    set_state(db, hp=3)
    result = engine.rest(db, rng)
    assert result.character.hp == result.character.max_hp
    assert result.character.inventory[REST_FUEL] == 1


def test_rest_without_rations_barely_helps(db: Database, rng: random.Random) -> None:
    """v0.1's rest() was a free, unlimited full heal, so losing was impossible."""
    hero(db, rng)
    for _ in range(2):  # spend both starting rations
        set_state(db, hp=2)
        engine.rest(db, rng)
    set_state(db, hp=2)
    result = engine.rest(db, rng)
    assert REST_FUEL not in result.character.inventory
    assert result.character.hp < result.character.max_hp
    assert result.character.hp == 2 + result.character.max_hp // 4


def test_rest_is_refused_mid_fight(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    set_state(db, hp=5)
    engine.attack(db, rng, "ogre")
    with pytest.raises(QuestError, match="mid-fight"):
        engine.rest(db, rng)


def test_rest_is_refused_at_full_health(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="already at full health"):
        engine.rest(db, rng)


def test_flee_ends_the_encounter_or_costs_a_blow(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    engine.attack(db, rng, "ogre")
    result = engine.flee(db, rng)
    if result.escaped:
        assert engine.encounter_view(db) is None
    else:
        assert result.damage_taken > 0
        assert engine.encounter_view(db) is not None


def test_flee_with_no_fight_is_an_error(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="nothing to flee"):
        engine.flee(db, rng)


# --------------------------------------------------------------------------
# Inventory, equipment, trade
# --------------------------------------------------------------------------


def test_equipping_a_weapon_changes_your_damage_dice(db: Database, rng: random.Random) -> None:
    """v0.1's inventory was inert: the rusty dagger did literally nothing."""
    hero(db, rng)
    assert engine.character_view(db).weapon_damage == str(WEAPONS["rusty dagger"].damage)
    engine.add_item(db, "battleaxe")
    result = engine.equip(db, "battleaxe")
    assert result.character.weapon == "battleaxe"
    assert result.character.weapon_damage == str(WEAPONS["battleaxe"].damage)


def test_you_cannot_equip_what_you_do_not_carry(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="not carrying"):
        engine.equip(db, "warhammer")


def test_you_cannot_equip_a_non_weapon(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="not a weapon"):
        engine.equip(db, "healing potion")


def test_buying_spends_gold_and_fills_the_pack(db: Database, rng: random.Random) -> None:
    """v0.1 accumulated gold with nothing whatsoever to spend it on."""
    hero(db, rng)
    set_state(db, gold=100)
    result = engine.buy(db, "healing potion", 2)
    assert result.character.gold == 40
    assert result.character.inventory["healing potion"] == 2


def test_buying_beyond_your_purse_is_refused(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="costs"):
        engine.buy(db, "flametongue")
    assert engine.character_view(db).gold == 10


def test_buying_something_unstocked_is_refused(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="does not stock"):
        engine.buy(db, "a castle")


def test_using_a_potion_heals(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    set_state(db, hp=4)
    engine.add_item(db, "healing potion")
    result = engine.use_item(db, rng, "healing potion")
    assert result.character.hp > 4
    assert "healing potion" not in result.character.inventory


def test_elixir_permanently_raises_max_hp(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    engine.add_item(db, "elixir of vigor")
    result = engine.use_item(db, rng, "elixir of vigor")
    assert result.character.max_hp == 23


def test_using_a_weapon_points_you_at_equip(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="call equip"):
        engine.use_item(db, rng, "battleaxe")


def test_using_an_item_you_lack_is_refused(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="Not enough"):
        engine.use_item(db, rng, "healing potion")


def test_selling_pays_out_and_empties_the_slot(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    engine.add_item(db, "wolf pelt", 2)
    result = engine.sell(db, "wolf pelt", 2)
    assert result.character.gold == 10 + 24
    assert "wolf pelt" not in result.character.inventory


def test_you_cannot_sell_the_weapon_in_your_hand(db: Database, rng: random.Random) -> None:
    hero(db, rng)
    with pytest.raises(QuestError, match="wielding"):
        engine.sell(db, "rusty dagger")


def test_inventory_is_per_save(db: Database, rng: random.Random) -> None:
    engine.create_character(db, rng, "Elowen", "rogue")
    engine.add_item(db, "bone charm", 3)
    engine.create_character(db, rng, "Bryn", "warrior")
    assert "bone charm" not in engine.character_view(db).inventory
    assert engine.character_view(db, "Elowen").inventory["bone charm"] == 3
