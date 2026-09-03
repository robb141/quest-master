# Quest Master — a text-RPG MCP server

A self-contained [MCP](https://modelcontextprotocol.io) server that turns Claude into a
Dungeon Master running a persistent, SQLite-backed text adventure. No API keys needed.

It is also a worked example of **six** MCP capabilities, because the game gives each one a
real job rather than a toy one:

| Capability | What it does here |
|---|---|
| **Tools** | `attack`, `flee`, `rest`, `equip`, `buy`, … — actions with consequences, each returning typed `structuredContent` |
| **Resources** | `quest://character`, `quest://log`, `quest://encounter`, `quest://bestiary`, `quest://shop` |
| **Resource templates** | `quest://character/{name}` — read any save slot without loading it |
| **Prompts** | `narrate_scene`, `recap_quest`, `plan_next_move` |
| **Sampling** | `describe_enemy` — the *server* asks the *client's* model for prose |
| **Elicitation** | swinging at very low HP asks the *player* to confirm first |
| **Completion** | argument autocomplete for prompt and template arguments |

Sampling and elicitation degrade gracefully: a client that does not advertise the
capability simply never sees them, and every tool still works.

## How it works

`quest-master` is an MCP server, not a program you interact with directly. An MCP client
(Claude Code) launches it as a subprocess and speaks JSON-RPC to it over stdio.

- The **server** owns the *mechanics*: dice, HP, encounters, gold/XP, level-ups, and
  persistence to `quest.db`.
- **Claude** owns the *story*: it decides which tool to call, then turns the result into
  narration and offers you choices.

A turn looks like: you say "I attack the goblin" → Claude calls `attack` → the server rolls,
updates `quest.db`, appends to the log, and returns both a narration string and a structured
result → Claude narrates it.

The important detail is that **the enemy's HP lives on the server**. Calling `attack` again
continues the same fight; the goblin stays wounded. That is what makes a fight winnable —
or losable.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

## Run it

```bash
quest-master                        # stdio (what an MCP client launches)
quest-master --transport streamable-http --port 8000
quest-master --db /tmp/scratch.db --seed 42   # throwaway save, reproducible dice
pytest                              # the test suite
```

`$QUEST_DB` and `$QUEST_SEED` do the same as `--db` and `--seed`.

## Connect it to Claude Code

```bash
claude mcp add quest-master -- $(pwd)/venv/bin/quest-master
```

Then start a new `claude` session and say something like:
*"Create a rogue named Elowen and start our quest."*

## Playing

Start with `create_character`. Fight things with `attack` — repeatedly, until something
dies. Read `quest://bestiary` to see what you are up against and `quest://shop` for what
gold buys.

The rules that matter:

- **Fights persist.** `attack` with an `enemy_name` starts a fight; `attack` with no
  arguments continues it. You cannot switch targets mid-fight — `flee` first.
- **Resting costs a ration.** With one you heal fully; without one you barely recover.
  You cannot rest mid-fight.
- **Death is real.** At 0 HP the character cannot act until `revive` — which costs half
  their gold — or you start a new save.
- **Gear matters.** Your equipped weapon sets your damage dice, so `buy` a better one and
  `equip` it. Potions heal, elixirs raise max HP, trinkets are just money.
- **Enemies are ranked.** Every monster declares the level from which it is a fair fight,
  and every fight reports a threat — trivial, fair, dangerous, deadly or hopeless — for
  the character as they stand. Read `quest://bestiary` before picking a fight.
- **Saves are separate.** `create_character` opens a new slot rather than overwriting;
  `list_characters` and `switch_character` move between them.

## Layout

```
tools/
  simulate_balance.py   plays thousands of real fights to measure win rates
src/quest_master/
  db.py        SQLite connection + ordered schema migrations
  content.py   bestiary, weapons, consumables, shop  (balance lives here)
  engine.py    game rules — no MCP, takes an explicit Database and Random
  models.py    typed tool results (the structuredContent schemas)
  server.py    the MCP surface
tests/         pytest, against throwaway databases with seeded dice
```

`server.py` at the repo root is a shim that keeps v0.1 MCP registrations working.

## Balance

The `recommended_level` on each monster is measured, not guessed —
`python tools/simulate_balance.py` plays 400 real fights per matchup through the engine
and reports win rate and median fight length:

```
monster              L1/rusty     L2/short     L4/battl     L6/warha
rat                 100%/   3    100%/   3    100%/   2    100%/   1
goblin               84%/   6     99%/   5    100%/   3    100%/   2
wolf                 58%/   7     94%/   6    100%/   4    100%/   3
bandit                8%/   7     48%/   8     98%/   6    100%/   4
dragon                0%/   2      0%/   3      0%/   4      0%/   5
```

The dragon is a genuine endgame fight: 20% at level 10, 94% at level 14.

## Upgrading from v0.1

An existing `quest.db` is migrated in place on first run — your character, gold, XP,
inventory and log are preserved and become save slot 1. The migration is covered by
[tests/test_migrations.py](tests/test_migrations.py).

Notable behaviour changes:

- `attack` no longer takes an `enemy_hp` argument; the encounter is server-side state.
- `create_character` no longer wipes your existing hero; it creates a new save slot.
- Invalid input (`roll_dice(sides=1)`, acting with no character) now raises a real tool
  error instead of returning a string that looks like success.
- Tools return structured objects, not bare strings. The prose is in the `narration` field.
