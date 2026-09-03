"""Typed tool results.

Every tool returns one of these instead of a bare string. ``MCPServer`` derives
an ``outputSchema`` from the return annotation and sends the value as
``structuredContent`` alongside the human-readable text, so the model no longer
has to parse HP out of an emoji sentence to know what happened.

Each model still carries a ``narration`` field: that is the prose the client
shows, and it is what the old string-returning tools produced.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CharacterView(BaseModel):
    """A character sheet as structured data."""

    name: str
    char_class: str = Field(description="Flavour only; classes are mechanically identical.")
    level: int
    hp: int
    max_hp: int
    xp: int
    xp_to_next_level: int = Field(description="Total XP required to reach the next level.")
    gold: int
    weapon: str
    weapon_damage: str = Field(description="Damage dice of the equipped weapon, e.g. '1d8'.")
    armor: int = Field(description="An enemy's attack roll must meet this to land a hit.")
    dead: bool
    inventory: dict[str, int] = Field(default_factory=dict)


class EncounterView(BaseModel):
    """The fight currently in progress, if any."""

    enemy_name: str
    monster_key: str
    hp: int
    max_hp: int
    tier: int
    rounds: int


class Rewards(BaseModel):
    xp: int
    gold: int
    loot: list[str] = Field(default_factory=list)


class DiceRoll(BaseModel):
    narration: str
    notation: str
    rolls: list[int]
    total: int


class CharacterResult(BaseModel):
    """Returned by tools whose whole effect is 'the sheet changed'."""

    narration: str
    character: CharacterView


class AttackResult(BaseModel):
    narration: str
    attack_roll: int
    hit: bool
    critical: bool
    damage_dealt: int
    enemy_defeated: bool
    counterattack_hit: bool = False
    damage_taken: int = 0
    character_died: bool = False
    levels_gained: int = 0
    rewards: Rewards | None = None
    encounter: EncounterView | None = Field(
        default=None, description="The ongoing fight, or null once the enemy is defeated or you fall."
    )
    character: CharacterView


class FleeResult(BaseModel):
    narration: str
    escaped: bool
    damage_taken: int = 0
    character_died: bool = False
    character: CharacterView


class ShopEntry(BaseModel):
    item: str
    price: int
    kind: str
    description: str


class SaveSlot(BaseModel):
    name: str
    char_class: str
    level: int
    hp: int
    max_hp: int
    dead: bool
    active: bool


class SaveList(BaseModel):
    narration: str
    characters: list[SaveSlot]
