# Quest Master — a text-RPG MCP server

A tiny, self-contained [MCP](https://modelcontextprotocol.io) server that turns
Claude into a Dungeon Master running a persistent (SQLite-backed) text adventure.
No API keys needed.

> ⚠️ **This project was vibecoded** — written quickly and conversationally with an
> AI assistant, optimised for "fun to poke at" rather than production robustness.
> Expect rough edges: single global character, a connection per query, no auth,
> no migrations. Read it as a demo of MCP's three building blocks, not a template.

## How it works

`server.py` is an MCP server, not a program you interact with directly. An MCP
client (Claude Code) launches it as a subprocess and speaks JSON-RPC to it over
stdio.

- The **server** owns the *mechanics*: dice math, HP, gold/XP, level-ups, and
  persistence to `quest.db`. Its tool return values are terse status strings.
- **Claude** (in the client) owns the *story*: it decides which tool to call
  based on what you type, then turns the status strings into narration and
  offers you choices.

So a turn looks like: you say "I attack the goblin" → Claude emits a structured
`attack` tool call with the args it inferred → the client sends it to the server
→ `attack()` rolls, updates `quest.db`, appends to the log, returns a status line
→ Claude narrates the result.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run it

```bash
source venv/bin/activate
python server.py          # runs the server directly (stdio transport)
python test_client.py     # smoke test: exercises every tool/resource
```

## Connect it to Claude Code

```bash
claude mcp add quest-master -- $(pwd)/venv/bin/python $(pwd)/server.py
```

Then start a new `claude` session and say something like:
"Create a rogue character named Elowen and start our quest."

## What it exposes

- **Tools** (actions the model can call): `create_character`, `roll_dice`,
  `attack`, `rest`, `add_item`
- **Resources** (read-only context): `quest://character`, `quest://log`
- **Prompts** (reusable templates): `narrate_scene`

Game state lives in `quest.db`, created on first run and **not** tracked by git
(it's your local save file).
