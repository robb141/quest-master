"""The MCP surface: tools, resources, prompts, sampling, elicitation, completion.

The original server demonstrated three MCP building blocks. This one uses six,
because the game gives each a natural job:

  TOOLS        actions with consequences (attack, buy, equip, ...), each
               returning a typed model so the client gets ``structuredContent``
               instead of prose it has to parse.
  RESOURCES    read-only context — the sheet, the log, the current fight, the
               bestiary, the shop — including a ``quest://character/{name}``
               template for peeking at other save slots.
  PROMPTS      reusable DM templates the user invokes.
  SAMPLING     the *server* asks the client's model for a monster description
               (``describe_enemy``) — the request travelling the other way.
  ELICITATION  the server asks the *player* to confirm a swing that is likely
               to kill them.
  COMPLETION   argument autocomplete for prompt and template arguments.

Sampling and elicitation degrade to a plain result when the connected client
does not advertise the capability, so the server stays usable everywhere.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from random import Random
from typing import Annotated, Any, TypeVar

import anyio.to_thread
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
    Sample,
)
from mcp_types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    CreateMessageResult,
    PromptReference,
    ResourceTemplateReference,
    SamplingMessage,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from quest_master import engine
from quest_master.content import CONSUMABLES, TRINKETS, WEAPONS
from quest_master.db import Database, connect
from quest_master.models import (
    AttackResult,
    CharacterResult,
    DiceRoll,
    FleeResult,
    SaveList,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "quest.db"

CHARACTER_URI = "quest://character"
LOG_URI = "quest://log"
ENCOUNTER_URI = "quest://encounter"

# Below this fraction of max HP, attacking asks the player to confirm.
RISKY_HP_FRACTION = 0.3


# --------------------------------------------------------------------------
# Server-wide state
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ServerState:
    db: Database
    rng: Random


# The lifespan hook owns this: it is created on startup and closed on shutdown,
# which is what replaced the old connect-per-query pattern.
#
# It is *also* reachable as a module global because MCPServer refuses Context
# injection on static (non-templated) resources, so `quest://character` has no
# other way to reach the database. Tools still receive Context for logging,
# progress, elicitation, and sampling.
_state: ServerState | None = None


def state() -> ServerState:
    if _state is None:  # pragma: no cover - only reachable outside a running server
        raise RuntimeError("Server state is not initialised; the lifespan hook has not run.")
    return _state


def db_path() -> Path:
    return Path(os.environ.get("QUEST_DB") or DEFAULT_DB_PATH)


def _make_rng() -> Random:
    """Seeded from QUEST_SEED when set, so tests get reproducible combat."""
    seed = os.environ.get("QUEST_SEED")
    return Random(int(seed)) if seed else Random()


@asynccontextmanager
async def lifespan(_app: MCPServer[ServerState]) -> AsyncIterator[ServerState]:
    global _state
    database = await anyio.to_thread.run_sync(partial(connect, db_path()))
    _state = ServerState(db=database, rng=_make_rng())
    try:
        yield _state
    finally:
        _state = None
        await anyio.to_thread.run_sync(database.close)


INSTRUCTIONS = """\
You are the Dungeon Master. The server owns the mechanics; you own the story.

Read quest://character before acting so you know the party's state, and
quest://encounter to see whether a fight is already underway. Call attack
repeatedly to resolve a fight round by round — the enemy's HP persists between
calls, so do not restart the fight each turn. If the character falls, only
revive() can continue that save. Turn each tool's `narration` into vivid prose
and offer the player two or three concrete choices.\
"""

mcp: MCPServer[ServerState] = MCPServer(
    "quest-master",
    title="Quest Master",
    version="0.2.0",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

T = TypeVar("T")


async def _call(fn: Callable[..., T], *args: Any) -> T:
    """Run a blocking engine call off the event loop."""
    return await anyio.to_thread.run_sync(partial(fn, *args))


async def _announce(ctx: Context[Any, ServerState], *uris: str) -> None:
    """Tell the client the named resources changed.

    Without this the client keeps showing a stale character sheet until it
    happens to re-read the resource. Delivery is best-effort: a client that
    never subscribed, or a transport with no back-channel, must not turn a
    successful game turn into a failed tool call.
    """
    for uri in uris:
        try:
            await ctx.notify_resource_updated(uri)
        except Exception as exc:
            logger.debug("could not notify %s: %s", uri, exc)


def _supports_elicitation(ctx: Context[Any, ServerState]) -> bool:
    caps = ctx.client_capabilities
    return caps is not None and caps.elicitation is not None


def _supports_sampling(ctx: Context[Any, ServerState]) -> bool:
    caps = ctx.client_capabilities
    return caps is not None and caps.sampling is not None


# --------------------------------------------------------------------------
# TOOLS — save management
# --------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(title="Create character", read_only_hint=False, destructive_hint=False))
async def create_character(name: str, char_class: str, ctx: Context[Any, ServerState]) -> CharacterResult:
    """Create a new adventurer in a fresh save slot and make it the active save.

    Unlike earlier versions this no longer overwrites your existing hero —
    saves live side by side. Class is flavour only; all classes play the same.
    """
    result = await _call(engine.create_character, state().db, state().rng, name, char_class)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="List saves", read_only_hint=True, idempotent_hint=True))
async def list_characters() -> SaveList:
    """List every save slot and show which one is active."""
    return await _call(engine.list_characters, state().db)


@mcp.tool(annotations=ToolAnnotations(title="Load save", read_only_hint=False, destructive_hint=False))
async def switch_character(name: str, ctx: Context[Any, ServerState]) -> CharacterResult:
    """Make an existing save the active one. All other tools act on the active save."""
    result = await _call(engine.switch_character, state().db, name)
    await _announce(ctx, CHARACTER_URI, LOG_URI, ENCOUNTER_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Delete save", read_only_hint=False, destructive_hint=True))
async def delete_character(name: str, ctx: Context[Any, ServerState]) -> SaveList:
    """Permanently delete a save slot. The adventure log is kept."""
    result = await _call(engine.delete_character, state().db, name)
    await _announce(ctx, CHARACTER_URI, LOG_URI, ENCOUNTER_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Revive at the shrine", read_only_hint=False))
async def revive(ctx: Context[Any, ServerState]) -> CharacterResult:
    """Bring a fallen character back at half HP for half their gold."""
    result = await _call(engine.revive, state().db)
    await _announce(ctx, CHARACTER_URI, LOG_URI, ENCOUNTER_URI)
    return result


# --------------------------------------------------------------------------
# TOOLS — dice
# --------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(title="Roll dice", read_only_hint=True, idempotent_hint=False))
async def roll_dice(sides: int = 20, count: int = 1) -> DiceRoll:
    """Roll `count` dice of `sides` faces for a skill check, saving throw, or random outcome.

    Invalid parameters raise a tool error rather than returning a message that
    looks like a successful roll.
    """
    return await _call(engine.roll_dice, state().rng, sides, count)


# --------------------------------------------------------------------------
# TOOLS — combat, with an elicitation guard
# --------------------------------------------------------------------------


class ConfirmRiskyAttack(BaseModel):
    """Schema the player is asked to fill in before a probably-fatal swing."""

    proceed: bool = Field(
        description="Press the attack anyway? The counterblow could kill you.",
    )


def _confirm_risky_attack(
    ctx: Context[Any, ServerState],
) -> ConfirmRiskyAttack | Elicit[ConfirmRiskyAttack]:
    """Resolver for `attack`'s hidden `confirm` parameter.

    Returning an :class:`Elicit` marker makes the framework ask the *player*;
    returning a plain model skips the question. The question is only raised when
    the character is badly hurt and the client actually supports elicitation, so
    ordinary swings cost no round trip and clients without the capability are
    unaffected.
    """
    proceed = ConfirmRiskyAttack(proceed=True)
    if not _supports_elicitation(ctx):
        return proceed
    try:
        char = engine.character_view(state().db)
    except engine.QuestError:
        return proceed  # no character; let the tool body raise the real error
    if char.dead or char.hp > max(1, int(char.max_hp * RISKY_HP_FRACTION)):
        return proceed
    return Elicit(
        f"{char.name} is at {char.hp}/{char.max_hp} HP. Another exchange could be fatal. "
        f"Attack anyway, or back off and rest()?",
        ConfirmRiskyAttack,
    )


@mcp.tool(annotations=ToolAnnotations(title="Attack", read_only_hint=False, idempotent_hint=False))
async def attack(
    ctx: Context[Any, ServerState],
    confirm: Annotated[ElicitationResult[ConfirmRiskyAttack], Resolve(_confirm_risky_attack)],
    enemy_name: str | None = None,
) -> AttackResult:
    """Resolve one exchange of blows in the current fight.

    Pass `enemy_name` to begin a new fight; omit it to continue the fight
    already in progress. The enemy's HP is stored server-side and carries over
    between calls, so a wounded enemy stays wounded. If the character is already
    in a fight, `enemy_name` is ignored — call flee() to disengage first.
    """
    if not isinstance(confirm, AcceptedElicitation) or not confirm.data.proceed:
        char = await _call(engine.character_view, state().db)
        return AttackResult(
            narration=(
                f"🛑 {char.name} holds back at {char.hp}/{char.max_hp} HP. "
                f"Nothing was resolved — rest(), use a healing potion, or flee()."
            ),
            attack_roll=0,
            hit=False,
            critical=False,
            damage_dealt=0,
            enemy_defeated=False,
            encounter=await _call(engine.encounter_view, state().db),
            character=char,
        )

    # MCP's logging capability is deprecated (SEP-2577), so server-side
    # diagnostics go to stderr, which is where an stdio client collects them.
    logger.info("resolving an attack round (enemy_name=%r)", enemy_name)
    result = await _call(engine.attack, state().db, state().rng, enemy_name)
    if result.encounter is not None:
        # Progress is expressed as the enemy's health bar draining.
        done = result.encounter.max_hp - result.encounter.hp
        await ctx.report_progress(done, result.encounter.max_hp, f"{result.encounter.enemy_name} wounded")
    await _announce(ctx, CHARACTER_URI, LOG_URI, ENCOUNTER_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Flee", read_only_hint=False, idempotent_hint=False))
async def flee(ctx: Context[Any, ServerState]) -> FleeResult:
    """Try to break off the current fight. Failing costs you a parting blow."""
    result = await _call(engine.flee, state().db, state().rng)
    await _announce(ctx, CHARACTER_URI, LOG_URI, ENCOUNTER_URI)
    return result


# --------------------------------------------------------------------------
# TOOLS — camp, inventory, trade
# --------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(title="Rest", read_only_hint=False))
async def rest(ctx: Context[Any, ServerState]) -> CharacterResult:
    """Make camp. Eating a ration heals you fully; without one you barely recover.

    Cannot be used during a fight.
    """
    result = await _call(engine.rest, state().db, state().rng)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Add item", read_only_hint=False))
async def add_item(item: str, ctx: Context[Any, ServerState], qty: int = 1) -> CharacterResult:
    """Put an item in the pack — quest rewards, story loot, anything you narrate."""
    result = await _call(engine.add_item, state().db, item, qty)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Use item", read_only_hint=False))
async def use_item(item: str, ctx: Context[Any, ServerState]) -> CharacterResult:
    """Drink a potion or elixir from the pack. See quest://shop for what is usable."""
    result = await _call(engine.use_item, state().db, state().rng, item)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Equip weapon", read_only_hint=False, idempotent_hint=True))
async def equip(item: str, ctx: Context[Any, ServerState]) -> CharacterResult:
    """Wield a weapon you are carrying. The equipped weapon sets your damage dice."""
    result = await _call(engine.equip, state().db, item)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Buy", read_only_hint=False))
async def buy(item: str, ctx: Context[Any, ServerState], qty: int = 1) -> CharacterResult:
    """Buy from the shop with gold. See quest://shop for the catalogue."""
    result = await _call(engine.buy, state().db, item, qty)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


@mcp.tool(annotations=ToolAnnotations(title="Sell", read_only_hint=False))
async def sell(item: str, ctx: Context[Any, ServerState], qty: int = 1) -> CharacterResult:
    """Sell something from the pack. Merchants pay a third of list price."""
    result = await _call(engine.sell, state().db, item, qty)
    await _announce(ctx, CHARACTER_URI, LOG_URI)
    return result


# --------------------------------------------------------------------------
# TOOLS — sampling: the server asks the client's model for prose
# --------------------------------------------------------------------------


def _flavour_request(enemy_name: str, ctx: Context[Any, ServerState]) -> Sample | None:
    """Resolver for `describe_enemy`.

    Returning a :class:`Sample` marker makes the framework issue a
    ``sampling/createMessage`` request to the *client*, inverting the usual
    direction: the server is asking the model for something. Returning ``None``
    when the client cannot sample keeps the tool working (it falls back to the
    bestiary stat line).
    """
    if not _supports_sampling(ctx):
        return None
    try:
        char = engine.character_view(state().db)
        context_line = f"The party is led by {char.name}, a level {char.level} {char.char_class}."
    except engine.QuestError:
        context_line = "The party has not yet been formed."
    return Sample(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=(
                        f"{context_line} In two or three sentences, describe the "
                        f"{enemy_name} the party has just encountered. Be atmospheric and "
                        f"concrete. Do not offer choices or mention dice."
                    ),
                ),
            )
        ],
        max_tokens=220,
        system_prompt="You write terse, vivid dark-fantasy prose for a tabletop RPG.",
    )


@mcp.tool(annotations=ToolAnnotations(title="Describe enemy", read_only_hint=True, open_world_hint=False))
async def describe_enemy(
    enemy_name: str,
    flavour: Annotated[CreateMessageResult | None, Resolve(_flavour_request)],
) -> str:
    """Get atmospheric prose for an enemy, plus its mechanical stat line.

    Where the client supports sampling, the description is generated by the
    client's own model at the server's request; otherwise the bestiary entry is
    returned on its own.
    """
    monster = await _call(engine.resolve_for_description, state().db, enemy_name)
    stats = (
        f"**{monster.name}** — tier {monster.tier}, {monster.hp} HP, armour {monster.armor}, "
        f"attack +{monster.attack_bonus}, damage {monster.damage}."
    )
    if flavour is None:
        return f"{stats}\n\n_(Client does not support sampling, so no generated description.)_"
    content = flavour.content
    prose = content.text if isinstance(content, TextContent) else "(non-text sampling result)"
    return f"{prose}\n\n{stats}"


# --------------------------------------------------------------------------
# RESOURCES
# --------------------------------------------------------------------------


def _render_sheet(char: Any) -> str:
    lines = [
        f"# {char.name} — level {char.level} {char.char_class}",
        f"HP: {char.hp}/{char.max_hp}" + ("  ☠️ **fallen — call revive()**" if char.dead else ""),
        f"XP: {char.xp} / {char.xp_to_next_level} to next level",
        f"Gold: {char.gold}",
        f"Weapon: {char.weapon} ({char.weapon_damage})",
        f"Armour: {char.armor}",
        "",
        "## Inventory",
    ]
    lines += [f"- {item} x{qty}" for item, qty in char.inventory.items()] or ["- (empty)"]
    return "\n".join(lines)


@mcp.resource(CHARACTER_URI, name="Character sheet", mime_type="text/markdown")
def character_sheet() -> str:
    """The active character's sheet: stats, equipment, and inventory."""
    try:
        return _render_sheet(engine.character_view(state().db))
    except engine.QuestError as exc:
        return str(exc)


@mcp.resource("quest://character/{name}", name="Character sheet by name", mime_type="text/markdown")
def character_sheet_by_name(name: str) -> str:
    """The sheet for a specific save slot, without switching to it."""
    try:
        return _render_sheet(engine.character_view(state().db, name))
    except engine.QuestError as exc:
        return str(exc)


@mcp.resource(LOG_URI, name="Adventure log", mime_type="text/markdown")
def adventure_log() -> str:
    """Everything that has happened to the active character, most recent first."""
    entries = engine.log_entries(state().db)
    if not entries:
        return "The adventure has not yet begun."
    return "\n".join(f"[{ts}] {entry}" for ts, entry in entries)


@mcp.resource(ENCOUNTER_URI, name="Current encounter", mime_type="text/markdown")
def current_encounter() -> str:
    """The fight in progress, if any — check this before calling attack."""
    try:
        enc = engine.encounter_view(state().db)
    except engine.QuestError as exc:
        return str(exc)
    if enc is None:
        return "No fight in progress."
    return (
        f"# Fighting: {enc.enemy_name}\n"
        f"Type: {enc.monster_key} (tier {enc.tier})\n"
        f"Enemy HP: {enc.hp}/{enc.max_hp}\n"
        f"Rounds so far: {enc.rounds}\n\n"
        f"Call attack() with no enemy_name to continue, or flee() to disengage."
    )


@mcp.resource("quest://bestiary", name="Bestiary", mime_type="text/markdown")
def bestiary() -> str:
    """Every named monster the engine knows, with its stats."""
    lines = [
        "# Bestiary",
        "",
        "| Monster | Tier | HP | Armour | Attack | Damage |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {m.name} | {m.tier} | {m.hp} | {m.armor} | +{m.attack_bonus} | {m.damage} |"
        for m in engine.bestiary_entries()
    ]
    lines += [
        "",
        "Any other enemy name is matched against these, or becomes a generic monster",
        "scaled to the character's level.",
    ]
    return "\n".join(lines)


@mcp.resource("quest://shop", name="Shop", mime_type="text/markdown")
def shop() -> str:
    """What gold can buy, and what loot is worth."""
    lines = ["# Shop", "", "## Weapons", "", "| Item | Price | Damage | Notes |", "|---|---|---|---|"]
    lines += [
        f"| {w.name} | {w.price or '—'} | {w.damage} | {w.description} |"
        for w in sorted(WEAPONS.values(), key=lambda w: w.price)
    ]
    lines += ["", "## Consumables", "", "| Item | Price | Effect |", "|---|---|---|"]
    lines += [
        f"| {c.name} | {c.price} | {c.description} |"
        for c in sorted(CONSUMABLES.values(), key=lambda c: c.price)
    ]
    lines += ["", "## Trinkets (sell only)", ""]
    lines += [f"- {item}: {value} gold" for item, value in sorted(TRINKETS.items())]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PROMPTS
# --------------------------------------------------------------------------

SETTING_SUGGESTIONS: tuple[str, ...] = (
    "a collapsed mine shaft",
    "a drowned chapel",
    "a frost-locked mountain pass",
    "a moonlit graveyard",
    "a smugglers' cove at low tide",
    "a thieves' market under the bridge",
    "the burnt-out watchtower",
    "the dragon's ossuary",
)


@mcp.prompt(title="Narrate a scene")
def narrate_scene(setting: str) -> str:
    """Narrate the next scene of the adventure as a Dungeon Master."""
    return (
        f"You are an imaginative Dungeon Master running a tabletop RPG. Read the "
        f"quest://character sheet and quest://encounter before you write, so the "
        f"scene matches the party's condition and any fight already underway. Then "
        f"vividly narrate what happens as the party enters: {setting}. Offer two or "
        f"three concrete choices, use roll_dice for uncertain outcomes, and call "
        f"attack only when the player commits to a fight."
    )


@mcp.prompt(title="Recap the quest so far")
def recap_quest() -> str:
    """Summarise everything that has happened, in the voice of a chronicler."""
    return (
        "Read quest://log and quest://character, then write a short chronicle of this "
        "adventure so far — how the hero has fared, what they carry, and what still "
        "threatens them. Two paragraphs at most, in the voice of a tavern storyteller."
    )


@mcp.prompt(title="Advise the player")
def plan_next_move() -> str:
    """Talk tactics: what the player should do next, given their actual numbers."""
    return (
        "Read quest://character, quest://encounter, quest://bestiary and quest://shop. "
        "Advise the player on their best next move using the real numbers: whether they "
        "can survive another exchange, whether to flee, rest, buy a better weapon, or "
        "press on. Be specific about which tool to call and why."
    )


# --------------------------------------------------------------------------
# COMPLETION — argument autocomplete
# --------------------------------------------------------------------------


# The SDK's completion decorator carries no annotations, so strict mypy
# treats anything it wraps as untyped.
@mcp.completion()  # type: ignore[untyped-decorator, no-untyped-call]
async def complete_argument(
    ref: PromptReference | ResourceTemplateReference,
    argument: CompletionArgument,
    context: CompletionContext | None,
) -> Completion | None:
    """Suggest settings for narrate_scene, and save names for the sheet template."""
    partial_value = argument.value.lower()
    if isinstance(ref, PromptReference) and ref.name == "narrate_scene" and argument.name == "setting":
        values = [s for s in SETTING_SUGGESTIONS if partial_value in s.lower()]
        return Completion(values=values[:10], total=len(values), has_more=False)
    if isinstance(ref, ResourceTemplateReference) and argument.name == "name":
        saves = await _call(engine.list_characters, state().db)
        values = [s.name for s in saves.characters if partial_value in s.name.lower()]
        return Completion(values=values[:10], total=len(values), has_more=False)
    return None
