"""Smoke test: talk to server.py over real MCP stdio protocol, like a client would."""

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="python", args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            resources = await session.list_resources()
            print("RESOURCES:", [str(r.uri) for r in resources.resources])

            prompts = await session.list_prompts()
            print("PROMPTS:", [p.name for p in prompts.prompts])

            print("\n--- create_character ---")
            r = await session.call_tool("create_character", {"name": "Elowen", "char_class": "rogue"})
            print(r.content[0].text)

            print("\n--- roll_dice ---")
            r = await session.call_tool("roll_dice", {"sides": 20, "count": 1})
            print(r.content[0].text)

            print("\n--- attack ---")
            r = await session.call_tool("attack", {"enemy_name": "goblin", "enemy_hp": 8})
            print(r.content[0].text)

            print("\n--- character_sheet resource ---")
            r = await session.read_resource("quest://character")
            print(r.contents[0].text)

            print("\n--- adventure_log resource ---")
            r = await session.read_resource("quest://log")
            print(r.contents[0].text)

            print("\nALL GOOD ✅")


asyncio.run(main())
