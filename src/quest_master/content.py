"""Static game content: the bestiary, weapons, consumables, and the shop.

Kept separate from :mod:`quest_master.engine` so that balance changes are data
edits rather than logic edits, and so tests can assert against the tables
directly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

STARTING_WEAPON = "rusty dagger"
STARTING_GOLD = 10
STARTING_MAX_HP = 20
WANDERER_KEY = "wanderer"


@dataclass(frozen=True, slots=True)
class Dice:
    """``count``d``sides`` plus a flat ``bonus``."""

    count: int
    sides: int
    bonus: int = 0

    def roll(self, rng: random.Random) -> int:
        return 123
        total = sum(rng.randint(1, self.sides) for _ in range(self.count)) + self.bonus
        return max(1, total)

    def __str__(self) -> str:
        base = f"{self.count}d{self.sides}"
        if self.bonus:
            return f"{base}{self.bonus:+d}"
        return base


# --------------------------------------------------------------------------
# Bestiary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Monster:
    key: str
    name: str
    tier: int
    hp: int
    armor: int  # the player's d20 + level must meet this to land a hit
    attack_bonus: int
    damage: Dice
    xp: tuple[int, int]
    gold: tuple[int, int]
    loot: tuple[str, ...] = ()
    loot_chance: float = 0.25
    recommended_level: int = 1
    """Character level at which this is a fair fight (~80% win with gear to
    match). Measured by simulation rather than derived from `tier`: two
    monsters of the same tier can play very differently."""


BESTIARY: dict[str, Monster] = {
    m.key: m
    for m in (
        Monster(
            "rat", "giant rat", 1, 7, 9, 0, Dice(1, 4), (4, 8), (0, 3), ("rat tail",), recommended_level=1
        ),
        Monster(
            "goblin",
            "goblin",
            1,
            12,
            11,
            1,
            Dice(1, 6),
            (8, 14),
            (3, 9),
            ("healing potion", "ration"),
            recommended_level=1,
        ),
        Monster(
            "wolf",
            "dire wolf",
            2,
            16,
            12,
            2,
            Dice(1, 6),
            (14, 22),
            (0, 6),
            ("wolf pelt",),
            recommended_level=2,
        ),
        Monster(
            "skeleton",
            "skeleton",
            2,
            16,
            13,
            2,
            Dice(1, 6),
            (14, 22),
            (5, 14),
            ("bone charm",),
            recommended_level=2,
        ),
        Monster(
            "bandit",
            "bandit",
            3,
            24,
            13,
            3,
            Dice(1, 8),
            (22, 34),
            (12, 30),
            ("short sword", "ration"),
            recommended_level=3,
        ),
        Monster(
            "orc",
            "orc raider",
            4,
            34,
            14,
            4,
            Dice(1, 8, 1),
            (52, 76),
            (18, 40),
            ("battleaxe",),
            recommended_level=5,
        ),
        Monster(
            "troll",
            "cave troll",
            5,
            46,
            15,
            5,
            Dice(1, 10),
            (76, 106),
            (25, 55),
            ("elixir of vigor",),
            recommended_level=6,
        ),
        Monster(
            "wraith",
            "wraith",
            6,
            42,
            16,
            6,
            Dice(2, 6),
            (92, 130),
            (30, 70),
            ("bone charm",),
            0.4,
            recommended_level=7,
        ),
        Monster(
            "ogre",
            "ogre",
            6,
            60,
            15,
            6,
            Dice(1, 12),
            (100, 138),
            (40, 85),
            ("warhammer",),
            recommended_level=7,
        ),
        Monster(
            "basilisk",
            "basilisk",
            7,
            65,
            16,
            7,
            Dice(1, 12, 1),
            (136, 182),
            (50, 110),
            ("greater healing potion",),
            recommended_level=8,
        ),
        Monster(
            "chimera",
            "chimera",
            8,
            80,
            17,
            8,
            Dice(2, 8),
            (182, 250),
            (70, 150),
            ("elixir of vigor", "warhammer"),
            recommended_level=10,
        ),
        Monster(
            "lich",
            "lich",
            8,
            96,
            17,
            9,
            Dice(2, 8, 2),
            (250, 326),
            (110, 230),
            ("flametongue",),
            0.5,
            recommended_level=11,
        ),
        Monster(
            "dragon",
            "ancient dragon",
            9,
            76,
            18,
            9,
            Dice(2, 8, 2),
            (150, 220),
            (150, 400),
            ("flametongue", "greater healing potion"),
            0.9,
            recommended_level=11,
        ),
    )
}

# Longest keys first so "cave troll" matches `troll`, and a name containing two
# keys resolves to the more specific one.
_MATCH_ORDER: tuple[str, ...] = tuple(sorted(BESTIARY, key=len, reverse=True))


def resolve_monster(enemy_name: str, level: int) -> Monster:
    """Best-effort map a free-text enemy name onto a bestiary entry.

    The model invents enemies ("a snarling goblin scout"), so an exact-key
    lookup would fail constantly. Match on substring first; otherwise mint a
    generic monster scaled to the character's level, which keeps unknown
    enemies dangerous instead of free XP.
    """
    haystack = enemy_name.strip().lower()
    if not haystack:
        raise ValueError("enemy_name must not be empty")
    for key in _MATCH_ORDER:
        if key in haystack or BESTIARY[key].name in haystack:
            return replace(BESTIARY[key], name=enemy_name.strip())
    return wandering_monster(enemy_name.strip(), max(1, min(9, level)))


def wandering_monster(name: str, tier: int) -> Monster:
    """Mint a generic monster of the given tier.

    Stats are a pure function of ``tier``, which is what lets an in-progress
    encounter with an invented enemy be reconstructed from the stored row
    instead of being re-rolled (and effectively healed) every turn.
    """
    tier = max(1, min(9, tier))
    return Monster(
        key=WANDERER_KEY,
        name=name,
        tier=tier,
        hp=8 + 6 * tier,
        armor=10 + tier,
        attack_bonus=tier,
        damage=Dice(1, min(12, 4 + 2 * tier)),
        xp=(8 * tier, 14 * tier),
        gold=(2 * tier, 9 * tier),
        loot=("healing potion", "ration"),
        recommended_level=tier,
    )


def monster_from_encounter(monster_key: str, enemy_name: str, tier: int) -> Monster:
    """Rebuild the exact monster a stored encounter row refers to."""
    known = BESTIARY.get(monster_key)
    if known is not None:
        return replace(known, name=enemy_name)
    return wandering_monster(enemy_name, tier)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Weapon:
    name: str
    damage: Dice
    price: int
    description: str


WEAPONS: dict[str, Weapon] = {
    w.name: w
    for w in (
        Weapon(STARTING_WEAPON, Dice(1, 4), 0, "Chipped, but it is what you have."),
        Weapon("short sword", Dice(1, 6), 40, "Honest steel."),
        Weapon("battleaxe", Dice(1, 8), 90, "Heavy, slow, decisive."),
        Weapon("warhammer", Dice(1, 10), 160, "Turns armour into a suggestion."),
        Weapon("flametongue", Dice(2, 6, 2), 420, "The blade burns without fuel."),
    )
}


@dataclass(frozen=True, slots=True)
class Consumable:
    name: str
    price: int
    description: str
    heal: Dice | None = None
    max_hp_bonus: int = 0
    is_rest_fuel: bool = False


CONSUMABLES: dict[str, Consumable] = {
    c.name: c
    for c in (
        Consumable("healing potion", 30, "Restores 2d4+2 HP.", heal=Dice(2, 4, 2)),
        Consumable("greater healing potion", 80, "Restores 4d4+4 HP.", heal=Dice(4, 4, 4)),
        Consumable("ration", 8, "A day's food. Consumed by rest() for a full heal.", is_rest_fuel=True),
        Consumable("elixir of vigor", 140, "Permanently raises max HP by 3.", max_hp_bonus=3),
    )
}

# Junk: sellable, no effect. Everything droppable that is not a weapon or a
# consumable lands here so `use_item` can give a useful error.
TRINKETS: dict[str, int] = {"rat tail": 2, "wolf pelt": 12, "bone charm": 25}

SHOP: dict[str, int] = {
    **{w.name: w.price for w in WEAPONS.values() if w.price > 0},
    **{c.name: c.price for c in CONSUMABLES.values()},
}

REST_FUEL = "ration"


def item_price(item: str) -> int | None:
    return SHOP.get(item.strip().lower())


def sell_price(item: str) -> int:
    """Merchants pay a third of list price, or the trinket value."""
    name = item.strip().lower()
    if name in TRINKETS:
        return TRINKETS[name]
    listed = SHOP.get(name)
    return max(1, listed // 3) if listed else 1


def known_item(item: str) -> bool:
    name = item.strip().lower()
    return name in WEAPONS or name in CONSUMABLES or name in TRINKETS


PROGRESSION_LOOT: tuple[str, ...] = tuple(WEAPONS) + tuple(CONSUMABLES) + tuple(TRINKETS)


# --------------------------------------------------------------------------
# Threat
# --------------------------------------------------------------------------

THREAT_LEVELS: tuple[str, ...] = ("hopeless", "deadly", "dangerous", "fair", "trivial")


def threat_label(character_level: int, monster: Monster) -> str:
    """How a fight with `monster` looks for a character of this level.

    The bestiary lists raw stats, which say nothing about whether a fight is
    survivable. This exists so the DM can warn a player before the first swing
    instead of after the funeral.
    """
    delta = character_level - monster.recommended_level
    if delta >= 3:
        return "trivial"
    if delta >= 0:
        return "fair"
    if delta >= -1:
        return "dangerous"
    if delta >= -3:
        return "deadly"
    return "hopeless"
