"""Measure win rate and fight length for each level/monster matchup.

Balance cannot be read off the stat block: it depends on hit chance, damage
dice and hit points compounding over a whole fight. This plays real fights
through the engine and reports what actually happens, which is where the
`recommended_level` values in `content.py` come from.

    python tools/simulate_balance.py
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quest_master import db as D
from quest_master import engine as E
from quest_master.content import BESTIARY

TRIALS = 400


def fight(level: int, weapon: str, monster_key: str, seed: int) -> tuple[bool, int]:
    db = D.connect(":memory:")
    rng = random.Random(seed)
    E.create_character(db, rng, "S", "rogue")
    with db.transaction() as c:
        cid = c.execute("SELECT character_id FROM active_save").fetchone()[0]
        hp = 20 + sum(rng.randint(1, 8) + 2 for _ in range(level - 1))
        c.execute(
            "UPDATE character SET level=?, hp=?, max_hp=?, weapon=? WHERE id=?", (level, hp, hp, weapon, cid)
        )
        c.execute("INSERT OR REPLACE INTO inventory VALUES (?,?,1)", (cid, weapon))
    rounds = 0
    r = E.attack(db, rng, BESTIARY[monster_key].name)
    while True:
        rounds += 1
        if r.enemy_defeated:
            db.close()
            return True, rounds
        if r.character_died:
            db.close()
            return False, rounds
        r = E.attack(db, rng)


def table(levels_weapons: list[tuple[int, str]]) -> None:
    heads = "".join(f"{f'L{lvl}/{weapon.split()[0][:5]}':>13}" for lvl, weapon in levels_weapons)
    print(f"{'monster':<16}{heads}")
    for key in BESTIARY:
        row = f"{key:<16}"
        for level, weapon in levels_weapons:
            wins, lens = 0, []
            for t in range(TRIALS):
                won, n = fight(level, weapon, key, t * 7919 + level)
                wins += won
                lens.append(n)
            row += f"{wins / TRIALS:>8.0%}/{statistics.median(lens):>4.0f}"
        print(row)


if __name__ == "__main__":
    print(f"win% / median rounds  ({TRIALS} trials)\n")
    table([(1, "rusty dagger"), (2, "short sword"), (4, "battleaxe"), (6, "warhammer")])


def progress_to(target_level: int, seed: int) -> tuple[int, int, int]:
    """Play a character from level 1 to `target_level`, fighting sensibly.

    Always takes the toughest monster that is still a fair fight, rests to full
    between fights, and buys the best weapon it can afford. Returns
    (fights, rounds, gold spent) — the real cost of the climb.
    """
    from quest_master.content import SHOP, WEAPONS

    database = D.connect(":memory:")
    rng = random.Random(seed)
    E.create_character(database, rng, "Climber", "rogue")
    fights = rounds = spent = 0

    while E.character_view(database).level < target_level:
        char = E.character_view(database)
        # Best affordable upgrade.
        for name, weapon in sorted(WEAPONS.items(), key=lambda kv: -kv[1].price):
            if weapon.price and weapon.price <= char.gold and name != char.weapon:
                if WEAPONS[name].damage.sides > WEAPONS[char.weapon].damage.sides:
                    E.buy(database, name, 1)
                    E.equip(database, name)
                    spent += SHOP[name]
                break
        # Toughest fair fight available.
        options = [m for m in BESTIARY.values() if m.recommended_level <= char.level]
        target = max(options, key=lambda m: m.recommended_level)
        with database.transaction() as conn:
            conn.execute("UPDATE character SET hp = max_hp, dead = 0")
        result = E.attack(database, rng, target.name)
        fights += 1
        while not (result.enemy_defeated or result.character_died):
            rounds += 1
            result = E.attack(database, rng)
        rounds += 1
        if fights > 5000:
            break
    database.close()
    return fights, rounds, spent


def progression_report(target_level: int = 14, trials: int = 12) -> None:
    runs = [progress_to(target_level, s) for s in range(trials)]
    fights = statistics.median(r[0] for r in runs)
    rounds = statistics.median(r[1] for r in runs)
    print(f"level 1 -> {target_level}: {fights:.0f} fights, {rounds:.0f} attack calls (median)")
