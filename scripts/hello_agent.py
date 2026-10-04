"""Smoke test: one dummy tool, one agent run. Proves the Agent SDK + your Claude login work."""
import asyncio
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock,
    create_sdk_mcp_server, query, tool,
)


@tool("get_time", "Get the current local date and time", {})
async def get_time(args):
    return {"content": [{"type": "text", "text": datetime.now().isoformat(timespec="minutes")}]}


async def main():
    server = create_sdk_mcp_server(name="demo", version="1.0.0", tools=[get_time])
    options = ClaudeAgentOptions(
        mcp_servers={"demo": server},
        allowed_tools=["mcp__demo__get_time"],
        tools=[],              # no built-in Claude Code tools; only ours
        setting_sources=[],    # ignore user/project Claude Code settings
        max_turns=4,
    )
    async for msg in query(prompt="What time is it? Use your tool.", options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    print(f"[tool call] {block.name}({block.input})")
        elif isinstance(msg, ResultMessage):
            print(f"[result] {msg.result}")
            print(f"[turns={msg.num_turns} error={msg.is_error}]")


asyncio.run(main())
