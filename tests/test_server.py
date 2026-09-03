"""End-to-end tests over the real MCP stdio protocol.

This replaces the old ``test_client.py``, which printed instead of asserting
and ran against the player's live ``quest.db``. Each test here launches the
server as a subprocess against a throwaway database with a fixed dice seed.
"""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.session import ClientRequestContext
from mcp.client.stdio import stdio_client

SAMPLED_TEXT = "The thing in the dark does not blink."


def _params(db_file: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "quest_master", "--transport", "stdio"],
        env={
            "QUEST_DB": str(db_file),
            "QUEST_SEED": "4242",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        },
    )


async def _accept_elicitation(
    context: ClientRequestContext, params: types.ElicitRequestParams
) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"proceed": True})


async def _decline_elicitation(
    context: ClientRequestContext, params: types.ElicitRequestParams
) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"proceed": False})


async def _sample(
    context: ClientRequestContext, params: types.CreateMessageRequestParams
) -> types.CreateMessageResult:
    return types.CreateMessageResult(
        role="assistant",
        content=types.TextContent(type="text", text=SAMPLED_TEXT),
        model="test-model",
        stop_reason="endTurn",
    )


@asynccontextmanager
async def session(
    db_file: Path,
    *,
    elicitation: Callable[..., Any] | None = None,
    sampling: Callable[..., Any] | None = None,
) -> AsyncIterator[ClientSession]:
    async with (
        stdio_client(_params(db_file)) as (read, write),
        ClientSession(read, write, elicitation_callback=elicitation, sampling_callback=sampling) as client,
    ):
        await client.initialize()
        yield client


def text_of(result: types.CallToolResult) -> str:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    return first.text


def data_of(result: types.CallToolResult) -> dict[str, Any]:
    """The tool's structuredContent, which v0.1 did not provide at all."""
    assert result.structured_content is not None, "tool returned no structured output"
    return dict(result.structured_content)


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "quest.db"


# --------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------


async def test_server_advertises_its_full_surface(db_file: Path) -> None:
    async with session(db_file) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        resources = {str(r.uri) for r in (await client.list_resources()).resources}
        templates = {t.uri_template for t in (await client.list_resource_templates()).resource_templates}
        prompts = {p.name for p in (await client.list_prompts()).prompts}

    assert {"create_character", "attack", "flee", "rest", "equip", "buy", "revive"} <= tools
    assert {
        "quest://character",
        "quest://log",
        "quest://encounter",
        "quest://bestiary",
        "quest://shop",
    } <= resources
    assert "quest://character/{name}" in templates
    assert {"narrate_scene", "recap_quest", "plan_next_move"} <= prompts


async def test_tools_declare_output_schemas_and_annotations(db_file: Path) -> None:
    async with session(db_file) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert tools["attack"].output_schema is not None
    assert "damage_dealt" in tools["attack"].output_schema["properties"]
    assert tools["list_characters"].annotations is not None
    assert tools["list_characters"].annotations.read_only_hint is True
    assert tools["delete_character"].annotations is not None
    assert tools["delete_character"].annotations.destructive_hint is True
    # Resolver-injected parameters must stay out of the model-facing schema.
    assert set(tools["attack"].input_schema["properties"]) == {"enemy_name"}
    assert set(tools["describe_enemy"].input_schema["properties"]) == {"enemy_name"}


# --------------------------------------------------------------------------
# Playing
# --------------------------------------------------------------------------


async def test_a_full_turn_returns_structured_state(db_file: Path) -> None:
    async with session(db_file) as client:
        made = await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
        assert made.is_error is not True
        character = data_of(made)["character"]
        assert character["name"] == "Elowen"
        assert character["hp"] == character["max_hp"] == 20

        first = data_of(await client.call_tool("attack", {"enemy_name": "ogre"}))
        assert first["encounter"]["hp"] < first["encounter"]["max_hp"]

        second = data_of(await client.call_tool("attack", {}))
        assert second["encounter"]["rounds"] == 2
        assert second["encounter"]["hp"] <= first["encounter"]["hp"]


async def test_resources_reflect_the_game_state(db_file: Path) -> None:
    async with session(db_file) as client:
        await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
        await client.call_tool("attack", {"enemy_name": "ogre"})

        sheet = (await client.read_resource("quest://character")).contents[0]
        assert isinstance(sheet, types.TextResourceContents)
        assert "Elowen" in sheet.text
        assert "rusty dagger" in sheet.text

        fight = (await client.read_resource("quest://encounter")).contents[0]
        assert isinstance(fight, types.TextResourceContents)
        assert "ogre" in fight.text

        log = (await client.read_resource("quest://log")).contents[0]
        assert isinstance(log, types.TextResourceContents)
        assert "Elowen" in log.text


async def test_character_template_reads_a_named_save(db_file: Path) -> None:
    async with session(db_file) as client:
        await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
        await client.call_tool("create_character", {"name": "Bryn", "char_class": "warrior"})

        active = (await client.read_resource("quest://character")).contents[0]
        other = (await client.read_resource("quest://character/Elowen")).contents[0]
        assert isinstance(active, types.TextResourceContents)
        assert isinstance(other, types.TextResourceContents)
        assert "Bryn" in active.text
        assert "Elowen" in other.text


async def test_rules_violations_come_back_as_tool_errors(db_file: Path) -> None:
    """v0.1 returned failures as ordinary strings, so the model could not tell."""
    async with session(db_file) as client:
        bad_roll = await client.call_tool("roll_dice", {"sides": 1, "count": 1})
        assert bad_roll.is_error is True

        no_character = await client.call_tool("attack", {"enemy_name": "rat"})
        assert no_character.is_error is True
        assert "create_character" in text_of(no_character)


async def test_prompts_render(db_file: Path) -> None:
    async with session(db_file) as client:
        rendered = await client.get_prompt("narrate_scene", {"setting": "a drowned chapel"})
        body = rendered.messages[0].content
        assert isinstance(body, types.TextContent)
        assert "drowned chapel" in body.text
        assert "quest://character" in body.text


# --------------------------------------------------------------------------
# Elicitation — the server asking the player
# --------------------------------------------------------------------------


async def _bring_to_death_s_door(client: ClientSession) -> None:
    await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
    # Fight an ogre until badly hurt, which is what trips the confirmation.
    for _ in range(40):
        result = await client.call_tool("attack", {"enemy_name": "ogre"})
        if result.is_error:
            return
        character = data_of(result)["character"]
        if character["dead"] or character["hp"] <= character["max_hp"] * 0.3:
            return


async def test_declining_the_risky_attack_prompt_resolves_nothing(db_file: Path) -> None:
    async with session(db_file, elicitation=_decline_elicitation) as client:
        await _bring_to_death_s_door(client)
        before = data_of(await client.call_tool("list_characters", {}))["characters"][0]
        held = await client.call_tool("attack", {})
        assert held.is_error is not True
        payload = data_of(held)
        assert payload["damage_dealt"] == 0
        assert payload["attack_roll"] == 0  # no roll happened at all
        assert payload["counterattack_hit"] is False
        after = data_of(await client.call_tool("list_characters", {}))["characters"][0]
        assert after["hp"] == before["hp"]


async def test_accepting_the_risky_attack_prompt_resolves_the_round(db_file: Path) -> None:
    async with session(db_file, elicitation=_accept_elicitation) as client:
        await _bring_to_death_s_door(client)
        pressed = await client.call_tool("attack", {})
        assert pressed.is_error is not True
        assert data_of(pressed)["attack_roll"] > 0  # the round actually resolved


async def test_a_client_without_elicitation_is_never_asked(db_file: Path) -> None:
    """No elicitation_callback means no capability; the swing must still resolve."""
    async with session(db_file) as client:
        await _bring_to_death_s_door(client)
        swung = await client.call_tool("attack", {})
        assert swung.is_error is not True
        assert data_of(swung)["attack_roll"] > 0


async def test_player_facing_text_never_leaks_implementation_details(db_file: Path) -> None:
    """Regression: the confirmation prompt and the declined-swing narration are
    rendered to the player verbatim, so a tool name or a mention of the server
    in them gets echoed back and breaks character.

    Hit points are deliberately allowed — a player deciding whether to press an
    attack needs the numbers, and stating them is ordinary table talk.
    """
    asked: list[str] = []

    async def capture(context: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"proceed": False})

    async with session(db_file, elicitation=capture) as client:
        await _bring_to_death_s_door(client)
        held = await client.call_tool("attack", {})
        narration = data_of(held)["narration"]

    assert asked, "the low-HP confirmation never fired"
    for text in (*asked, narration):
        assert "()" not in text, f"leaks a function name: {text!r}"
        assert "quest://" not in text, f"leaks a resource URI: {text!r}"
        lowered = text.lower()
        for banned in ("server", "tool", "tools", "function", "mcp", "structured"):
            assert not re.search(rf"\b{banned}\b", lowered), f"leaks {banned!r}: {text!r}"
        # ...and the numbers the player needs are present.
        assert re.search(r"\d+/\d+", text), f"no hit points shown: {text!r}"


# --------------------------------------------------------------------------
# Sampling — the server asking the client's model
# --------------------------------------------------------------------------


async def test_describe_enemy_uses_client_sampling_when_available(db_file: Path) -> None:
    async with session(db_file, sampling=_sample) as client:
        await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
        described = await client.call_tool("describe_enemy", {"enemy_name": "ancient dragon"})
        assert described.is_error is not True
        body = text_of(described)
        assert SAMPLED_TEXT in body
        assert "tier 9" in body


async def test_describe_enemy_falls_back_without_sampling(db_file: Path) -> None:
    async with session(db_file) as client:
        described = await client.call_tool("describe_enemy", {"enemy_name": "goblin"})
        assert described.is_error is not True
        body = text_of(described)
        assert SAMPLED_TEXT not in body
        assert "does not support sampling" in body
        assert "12 HP" in body


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------


async def test_completion_suggests_settings_and_save_names(db_file: Path) -> None:
    async with session(db_file) as client:
        await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})

        settings = await client.complete(
            types.PromptReference(type="ref/prompt", name="narrate_scene"),
            {"name": "setting", "value": "chap"},
        )
        assert "a drowned chapel" in settings.completion.values

        saves = await client.complete(
            types.ResourceTemplateReference(type="ref/resource", uri="quest://character/{name}"),
            {"name": "name", "value": "elo"},
        )
        assert saves.completion.values == ["Elowen"]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


async def test_state_survives_a_server_restart(db_file: Path) -> None:
    """The point of the save file: close the server, reopen, keep playing."""
    async with session(db_file) as client:
        await client.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
        await client.call_tool("attack", {"enemy_name": "ogre"})
        mid_fight = data_of(await client.call_tool("attack", {}))["encounter"]

    async with session(db_file) as client:
        fight = (await client.read_resource("quest://encounter")).contents[0]
        assert isinstance(fight, types.TextResourceContents)
        assert f"{mid_fight['hp']}/{mid_fight['max_hp']}" in fight.text
        resumed = data_of(await client.call_tool("attack", {}))
        assert resumed["encounter"] is None or resumed["encounter"]["rounds"] == 3
