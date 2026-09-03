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
